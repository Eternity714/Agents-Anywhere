"""Read explicit App membership; cwd and scratch directory names are not identity."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_APP_STATE_BYTES = 8 * 1024 * 1024


def read_app_project_memberships(codex_home: str) -> dict[str, bool | None]:
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
