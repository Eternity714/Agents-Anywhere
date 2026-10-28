from __future__ import annotations

import asyncio
import json

import pytest
from test_codex_runtime import FakeCodexClient, FakeHost, _config

from connector.core.config import ConnectorConfig
from connector.runtimes.codex.runtime import CodexRuntime
from connector.runtimes.codex.sessions.project_membership import (
    read_app_project_memberships,
)
from connector.server.runtime_sync import RuntimeSyncRunner


@pytest.mark.parametrize(
    ("native", "expected"),
    [({}, None), ({"projectId": None}, None), ({"projectId": "p1"}, True),
     ({"projectId": ""}, None), ({"projectId": False}, None),
     ({"projectId": []}, None), ({"projectId": "  "}, None)],
)
def test_reader_reports_only_explicit_valid_project_membership(native, expected):
    async def run():
        client = FakeCodexClient()
        client.results["thread/list"] = {"threads": [
            {"id": "thread_1", "cwd": "/scratch/she", **native},
        ]}
        runtime = CodexRuntime(config=_config(), host=FakeHost(), client=client)
        session, = await runtime.list_sessions()
        assert session.cwd == "/scratch/she"
        if expected is None:
            assert "nativeProjectAssigned" not in session.metadata
        else:
            assert session.metadata["nativeProjectAssigned"] is expected

    asyncio.run(run())


@pytest.mark.parametrize(("state", "expected"), [
    ({"projectless-thread-ids": ["thread_1"]}, False),
    ({"projectless-thread-ids": ["thread_1"], "local-projects": {"p1": {"id": "p1"}},
      "thread-project-assignments": {"thread_1": {"projectKind": "local", "projectId": "p1"}}}, True),
    ({"projectless-thread-ids": ["thread_1"],
      "thread-project-assignments": {"thread_1": {"projectKind": "remote", "projectId": "p1"}}}, None),
    ({"projectless-thread-ids": ["thread_1", 123]}, None),
    ({"thread-projectless-output-directories": {"thread_1": "/scratch/she"}}, None),
])
def test_app_membership_is_explicit_and_conflicting_assignments_are_protected(tmp_path, state, expected):
    async def run():
        (tmp_path / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
        client = FakeCodexClient()
        client.results["thread/list"] = {"threads": [{
            "id": "thread_1", "cwd": "/scratch/she", "projectId": None,
        }]}
        config = _config()
        config.values["codexHome"] = str(tmp_path)
        runtime = CodexRuntime(config=config, host=FakeHost(), client=client)
        session, = await runtime.list_sessions()
        assert session.metadata.get("nativeProjectAssigned") is expected

    asyncio.run(run())


def test_membership_change_with_unchanged_history_still_sends_metadata(tmp_path):
    async def run():
        state_path = tmp_path / ".codex-global-state.json"
        state_path.write_text('{}', encoding="utf-8")
        client = FakeCodexClient()
        client.results["thread/list"] = {"threads": [{
            "id": "thread_1", "cwd": "/scratch/she", "updatedAt": 123,
        }]}
        host = FakeHost()
        config = _config()
        config.values["codexHome"] = str(tmp_path)
        runtime = CodexRuntime(config=config, host=host, client=client)
        first, = await runtime.list_sessions()
        pending_key, pending_state = runtime._session_reader._pending_sync_states[first.session_id]
        host.sync_states[pending_key] = pending_state
        host.sync_states["codex/timeline-sync/thread_1"] = {
            "version": 1, "sessionId": first.session_id, "items": {},
        }
        before, = await runtime.list_sessions()
        assert before.metadata["sync"]["changed"] is False
        host.sync_states["codex/project-membership/thread_1"] = {"assigned": 0}
        state_path.write_text(json.dumps({"projectless-thread-ids": ["thread_1"]}), encoding="utf-8")
        after, = await runtime.list_sessions()
        assert after.metadata["nativeProjectAssigned"] is False
        assert after.metadata["sync"]["changed"] is True
        assert after.metadata["sync"]["requires_timeline_sync"] is False
        # Listing is not delivery. A failed upload must retry the observation.
        retry, = await runtime.list_sessions()
        assert retry.metadata["sync"]["changed"] is True
        delivered = []
        fail_upload = True

        async def ingest(notifications):
            if fail_upload:
                raise RuntimeError("upload failed")
            delivered.extend(notifications)

        runner = RuntimeSyncRunner(
            config=ConnectorConfig("http://test", "conn_test", "test-only"),
            supervisor=None, host=host, preferences_reader=dict,
            send_notification=None, ingest_notifications=ingest,
        )
        with pytest.raises(RuntimeError, match="upload failed"):
            await runner.sync_existing_session(runtime, after)
        retry, = await runtime.list_sessions()
        assert retry.metadata["sync"]["changed"] is True
        fail_upload = False
        await runner.sync_existing_session(runtime, retry)
        assert [notice["method"] for notice in delivered] == ["session.meta.upsert"]
        assert delivered[0]["params"]["metadata"]["nativeProjectAssigned"] is False
        repeated, = await runtime.list_sessions()
        assert repeated.metadata["sync"]["changed"] is False

    asyncio.run(run())


@pytest.mark.parametrize("raw", [b'{', b'[]', b'\xff', b'{"projectless-thread-ids":null}'])
def test_damaged_app_state_is_unknown(tmp_path, raw):
    (tmp_path / ".codex-global-state.json").write_bytes(raw)
    assert read_app_project_memberships(str(tmp_path)) == {}


def test_newer_scan_before_timeline_prepare_cannot_ack_the_wrong_membership(tmp_path):
    async def run():
        state_path = tmp_path / ".codex-global-state.json"
        state_path.write_text(json.dumps({"projectless-thread-ids": ["thread_1"]}), encoding="utf-8")
        host = FakeHost()
        config = _config()
        config.values["codexHome"] = str(tmp_path)
        client = FakeCodexClient()
        client.results["thread/list"] = {"threads": [{
            "id": "thread_1", "cwd": "/repo", "updatedAt": 123,
        }]}
        runtime = CodexRuntime(config=config, host=host, client=client)
        old, = await runtime.list_sessions()
        assert old.metadata["sync"]["requires_timeline_sync"] is True
        state_path.write_text(json.dumps({
            "local-projects": {"p1": {"id": "p1"}},
            "thread-project-assignments": {"thread_1": {"projectKind": "local", "projectId": "p1"}},
        }), encoding="utf-8")
        newer, = await runtime.list_sessions()
        assert newer.metadata["nativeProjectAssigned"] is True
        delivered = []

        async def ingest(notifications):
            delivered.extend(notifications)

        runner = RuntimeSyncRunner(
            config=ConnectorConfig("http://test", "conn_test", "test-only"),
            supervisor=None, host=host, preferences_reader=dict,
            send_notification=None, ingest_notifications=ingest,
        )
        await runner.sync_existing_session(runtime, old)
        meta = next(notice for notice in delivered if notice["method"] == "session.meta.upsert")
        assert meta["params"]["metadata"]["nativeProjectAssigned"] is False
        assert host.sync_states["codex/project-membership/thread_1"] == {"assigned": False}
        following, = await runtime.list_sessions()
        assert following.metadata["nativeProjectAssigned"] is True
        assert following.metadata["sync"]["changed"] is True
        assert following.metadata["sync"]["requires_timeline_sync"] is False
        await runner.sync_existing_session(runtime, following)
        acknowledged, = await runtime.list_sessions()
        assert acknowledged.metadata["sync"]["changed"] is False
        # A torn App write cannot erase the last confirmed observation.
        state_path.write_bytes(b'{')
        unknown, = await runtime.list_sessions()
        assert "nativeProjectAssigned" not in unknown.metadata
        assert host.sync_states["codex/project-membership/thread_1"] == {"assigned": True}

    asyncio.run(run())


def test_app_state_reader_limits_input_and_tolerates_permissions(tmp_path, monkeypatch):
    from pathlib import Path

    from connector.runtimes.codex.sessions import project_membership

    (tmp_path / ".codex-global-state.json").write_bytes(b' ' * 257)
    monkeypatch.setattr(project_membership, "MAX_APP_STATE_BYTES", 256)
    assert read_app_project_memberships(str(tmp_path)) == {}

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", denied)
    assert read_app_project_memberships(str(tmp_path)) == {}
