from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from types import SimpleNamespace

import pytest
from test_codex_runtime import FakeCodexClient, FakeHost, _config

from connector.core.config import ConnectorConfig
from connector.runtimes.codex.runtime import CodexRuntime
from connector.runtimes.codex.sessions.project_membership import (
    read_app_project_memberships,
)
from connector.server.runtime_sync import RuntimeSyncRunner


def app_state(tmp_path, **overrides):
    state = {"thread-project-assignments": {}, "local-projects": {}, **overrides}
    (tmp_path / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")


def native_db(tmp_path, rows, name="state_5.sqlite"):
    connection = sqlite3.connect(tmp_path / name)
    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT)")
    connection.executemany("INSERT INTO threads VALUES (?, ?)", rows)
    connection.commit()
    return connection


def test_old_threads_missing_from_both_app_lists_use_native_membership(tmp_path):
    app_state(tmp_path, **{
        "local-projects": {"legacy": {"id": "legacy"}},
        "thread-project-assignments": {
            "assigned": {"projectKind": "local", "projectId": "legacy"},
            "remote": {"projectKind": "remote", "projectId": "other"},
        },
        "projectless-thread-ids": ["moved"],
    })
    rows = [("old", None), ("assigned", None), ("remote", None), ("moved", "native")]
    native_db(tmp_path, rows).close()
    result = read_app_project_memberships(str(tmp_path), tuple(key for key, _ in rows) + ("missing",))
    assert result == {"old": False, "assigned": True, "remote": None, "moved": True}


def test_reader_reports_historical_projectless_thread_with_unchanged_cwd(tmp_path):
    app_state(tmp_path)
    native_db(tmp_path, [("thread_1", None)]).close()

    async def run():
        config = _config()
        config.values["codexHome"] = str(tmp_path)
        client = FakeCodexClient()
        client.results["thread/list"] = {"threads": [{
            "id": "thread_1", "cwd": "/old-output/9-6", "projectId": None,
        }]}
        runtime = CodexRuntime(config=config, host=FakeHost(), client=client)
        session, = await runtime.list_sessions()
        assert session.metadata["nativeProjectAssigned"] is False
        assert session.cwd == "/old-output/9-6"

    asyncio.run(run())


@pytest.mark.parametrize("state", [{}, {"local-projects": {}},
    {"thread-project-assignments": {}}, {"thread-project-assignments": [], "local-projects": {}},
    {"thread-project-assignments": {}, "local-projects": []}])
def test_native_null_without_understood_legacy_registries_is_unknown(tmp_path, state):
    (tmp_path / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
    native_db(tmp_path, [("old", None)]).close()
    assert "old" not in read_app_project_memberships(str(tmp_path), ("old",))


@pytest.mark.parametrize("failure", ["missing", "old_schema", "corrupt", "newer_corrupt", "locked"])
def test_unavailable_native_state_never_erases_previous_evidence(tmp_path, failure):
    app_state(tmp_path, **{"projectless-thread-ids": ["explicit"]})
    held = None
    if failure == "old_schema":
        held = sqlite3.connect(tmp_path / "state_5.sqlite")
        held.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        held.commit()
    elif failure == "corrupt":
        (tmp_path / "state_5.sqlite").write_bytes(b"damaged")
    elif failure == "newer_corrupt":
        native_db(tmp_path, [("old", None)]).close()
        (tmp_path / "state_10.sqlite").write_bytes(b"damaged")
    elif failure == "locked":
        held = native_db(tmp_path, [("old", None)])
        held.execute("BEGIN EXCLUSIVE")
    try:
        assert read_app_project_memberships(str(tmp_path), ("old",)) == {"explicit": False}
        if failure == "missing":
            assert list(tmp_path.glob("*.sqlite")) == []
    finally:
        if held is not None:
            held.close()


def test_native_reader_observes_live_wal_and_binds_thread_ids(tmp_path):
    app_state(tmp_path)
    conn = native_db(tmp_path, [])
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        malicious_id = "x') OR 1=1--"
        conn.executemany("INSERT INTO threads VALUES (?, ?)", [(malicious_id, None), ("other", "p")])
        conn.commit()
        before = (tmp_path / "state_5.sqlite-wal").read_bytes()
        assert read_app_project_memberships(str(tmp_path), (malicious_id,)) == {malicious_id: False}
        assert (tmp_path / "state_5.sqlite-wal").read_bytes() == before
    finally:
        conn.close()


def test_membership_delivery_precedes_blocked_history_and_retries_failed_upload(tmp_path):
    async def run():
        app_state(tmp_path)
        native_db(tmp_path, [("old", None), ("assigned", "p")]).close()
        config = _config()
        config.values["codexHome"] = str(tmp_path)
        client = FakeCodexClient()
        client.results["thread/list"] = {"threads": [
            {"id": "old", "cwd": "/old/9-6"}, {"id": "assigned", "cwd": "/repo"},
        ]}
        host = FakeHost()
        runtime = CodexRuntime(config=config, host=host, client=client)
        supervisor = SimpleNamespace(
            runtimes=("codex",), resolve_runtime=lambda _: runtime,
            entry=lambda _: SimpleNamespace(runtime_type="codex", runtime_id="codex"),
        )
        delivered = []
        fail = True

        async def ingest(notifications):
            if fail and any(item["method"] == "session.meta.upsert" for item in notifications):
                raise RuntimeError("upload failed")
            delivered.extend(notifications)

        runner = RuntimeSyncRunner(
            config=ConnectorConfig("http://test", "conn_test", "test-only"),
            supervisor=supervisor, host=host, preferences_reader=dict,
            send_notification=None, ingest_notifications=ingest,
        )
        async def skip_catalogs(_runtime):
            pass

        runner.push_runtime_catalogs = skip_catalogs
        started = asyncio.Event()

        async def blocked_history(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        runner.sync_existing_session = blocked_history
        await runner.sync_existing_once()
        assert "codex/project-membership/old" not in host.sync_states
        assert not started.is_set()
        fail = False
        task = asyncio.create_task(runner.sync_existing_once())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            observations = [item["params"]["metadata"]["nativeProjectAssigned"]
                for item in delivered if item["method"] == "session.meta.upsert"]
            assert observations == [False, True]
            assert host.sync_states["codex/project-membership/old"] == {"assigned": False}
            assert host.sync_states["codex/project-membership/assigned"] == {"assigned": True}
            assert not any(key.startswith("codex/timeline-sync/") for key in host.sync_states)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())
