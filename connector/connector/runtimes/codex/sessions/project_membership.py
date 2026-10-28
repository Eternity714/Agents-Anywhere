"""Read explicit App membership; cwd and scratch directory names are not identity."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

MAX_APP_STATE_BYTES = 8 * 1024 * 1024


def _read_native_project_memberships(
    codex_home: str, thread_ids: Sequence[str],
) -> dict[str, bool | None]:
    """Read only requested threads from the newest native schema, including WAL."""
    if not thread_ids:
        return {}
    try:
        candidates = [
            path for path in Path(codex_home).glob("state_*.sqlite")
            if path.stem.removeprefix("state_").isdigit() and path.is_file()
        ]
        if not candidates:
            return {}
        database = max(candidates, key=lambda path: int(path.stem.removeprefix("state_")))
        result: dict[str, bool | None] = {}
        # Never create or migrate Codex's database, nor ignore its live WAL.
        with closing(sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1,
        )) as conn:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            for offset in range(0, len(thread_ids), 500):
                batch = thread_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT id, project_id FROM threads WHERE id IN ({placeholders})", batch,
                )
                for thread_id, project_id in rows:
                    result[thread_id] = (
                        False if project_id is None else True
                        if isinstance(project_id, str) and project_id.strip() else None
                    )
        return result
    except (OSError, sqlite3.Error, ValueError):
        # Busy, damaged, missing-column or unreadable databases are unknown.
        # Do not fall back to an older schema after a failed current read.
        return {}


def read_app_project_memberships(
    codex_home: str, thread_ids: Sequence[str] = (),
) -> dict[str, bool | None]:
    try:
        with (Path(codex_home) / ".codex-global-state.json").open("rb") as source:
            raw = source.read(MAX_APP_STATE_BYTES + 1)
        if len(raw) > MAX_APP_STATE_BYTES:
            return {}
        state = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        # A missing, truncated or unreadable App state is unknown, never empty truth.
        return {}
    if not isinstance(state, dict):
        return {}
    assignments = state.get("thread-project-assignments", {})
    projectless = state.get("projectless-thread-ids", [])
    projects = state.get("local-projects", {})
    if (
        not isinstance(assignments, dict)
        or not isinstance(projectless, list)
        or any(not isinstance(key, str) or not key for key in projectless)
    ):
        return {}
    result: dict[str, bool | None] = dict.fromkeys(projectless, False)
    for thread_id, assignment in assignments.items():
        # Any assignment blocks a conflicting projectless entry. Only a valid
        # current local project confirms membership; remote/stale entries are unknown.
        result[thread_id] = None
        if not isinstance(assignment, dict) or not isinstance(projects, dict):
            continue
        project_id = assignment.get("projectId")
        if (
            assignment.get("projectKind") == "local"
            and isinstance(project_id, str)
            and project_id
            and isinstance(projects.get(project_id), dict)
            and projects[project_id].get("id") == project_id
        ):
            result[thread_id] = True
    # Old threads can be absent from both legacy lists. A native NULL supplies
    # the missing observation, but legacy assignments still take precedence:
    # Codex can add project_id before migrating those assignments into SQLite.
    # Require both legacy registries to be present and valid before interpreting
    # absence as meaningful. Truncated/unsupported App state stays unknown.
    if (
        "thread-project-assignments" in state
        and isinstance(state.get("local-projects"), dict)
    ):
        for thread_id, assigned in _read_native_project_memberships(codex_home, thread_ids).items():
            if assigned is True:
                result[thread_id] = True
            elif thread_id not in result and assigned is False:
                result[thread_id] = False
    return result


def project_membership_metadata(
    thread_id: str, thread: Mapping[str, Any], app_memberships: Mapping[str, bool | None],
) -> dict[str, bool]:
    native_project = thread.get("projectId")
    if isinstance(native_project, str) and native_project.strip():
        return {"nativeProjectAssigned": True}
    # Native null is also returned for App projects kept in the legacy local
    # registry. It cannot by itself prove that a thread is projectless.
    observed = app_memberships.get(thread_id)
    return {"nativeProjectAssigned": observed} if type(observed) is bool else {}
