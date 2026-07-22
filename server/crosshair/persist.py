"""On-disk workspace persistence: current state, the mutation log, and data arrays.

The daemon holds the workspace in memory and is the only writer here. Three
artifacts live under `~/.crosshair/workspace` (or `$CROSSHAIR_HOME`):

    state.json          the current workspace, rewritten after every mutation
    history.jsonl       append-only record of every mutation, one JSON per line
    data/<ref_id>.json  materialized $ref arrays, so restored specs still render

`state.json` is what makes plots survive a restart; `history.jsonl` is what makes
them reproducible — it keeps the arguments (and, when the agent supplies it, the
code) behind every panel that has ever been on screen, including revisions the
current state has long since overwritten.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from .daemon import RUNTIME_DIR

WORKSPACE_DIR = RUNTIME_DIR / "workspace"
STATE_FILE = WORKSPACE_DIR / "state.json"
HISTORY_FILE = WORKSPACE_DIR / "history.jsonl"
DATA_DIR = WORKSPACE_DIR / "data"

# State is rewritten whole, and a streaming panel can mutate many times a second,
# so saves are coalesced rather than run inline with the op.
SAVE_DEBOUNCE_S = 1.0

_save_task: asyncio.Task | None = None
_history_seq = 0


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _write_atomic(path: Path, text: str) -> None:
    """Write via tmp+rename so a crash mid-write can't leave a truncated file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---------- mutation history ----------


def record(
    op: str,
    args: dict | None = None,
    *,
    view: str | None = None,
    panel_id: str | None = None,
    rev: int | None = None,
    code: str | None = None,
    snapshot: dict | None = None,
) -> None:
    """Append one mutation to the history log. Never raises — history is not the job.

    `snapshot` is the panel's full resolved state after the op, stored only for
    version-defining edits. It is what a restore reapplies, and it is serialized
    here and now, so later in-place edits to the live spec cannot alter it.
    """
    global _history_seq
    _history_seq += 1
    entry = {
        "seq": _history_seq,
        "ts": time.time(),
        "op": op,
        "view": view,
        "panel_id": panel_id,
        "rev": rev,
        "code": code,
        "args": args or {},
        "snapshot": snapshot,
    }
    try:
        _ensure_dirs()
        with HISTORY_FILE.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def read_history(
    view: str | None = None,
    panel_id: str | None = None,
    limit: int = 50,
    include_args: bool = False,
) -> list[dict]:
    """Most recent `limit` entries, oldest first, optionally filtered to a tab or panel.

    `args` and the panel `snapshot` both carry whole figure specs, so they are
    dropped from the listing — the snapshot becomes a lightweight `restorable`
    flag. Fetch the full snapshot for a single entry with `get_history_entry`.
    """
    try:
        lines = HISTORY_FILE.read_text().splitlines()
    except FileNotFoundError:
        return []

    entries: list[dict] = []
    # Walk backwards so `limit` keeps the newest entries, not the oldest.
    for line in reversed(lines):
        if len(entries) >= limit:
            break
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if view is not None and entry.get("view") != view:
            continue
        if panel_id is not None and entry.get("panel_id") != panel_id:
            continue
        entry["restorable"] = bool(entry.get("snapshot"))
        entry.pop("snapshot", None)
        if not include_args:
            entry.pop("args", None)
        entries.append(entry)
    entries.reverse()
    return entries


def get_history_entry(seq: int) -> dict | None:
    """The full entry for one seq, snapshot included — the source for a restore."""
    try:
        lines = HISTORY_FILE.read_text().splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("seq") == seq:
            return entry
    return None


def _resume_seq() -> None:
    """Continue the history sequence across daemon restarts rather than restarting at 1."""
    global _history_seq
    try:
        lines = HISTORY_FILE.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in reversed(lines):
        try:
            _history_seq = int(json.loads(line).get("seq") or 0)
            return
        except (json.JSONDecodeError, TypeError, ValueError):
            continue


# ---------- current state ----------


def save_state(state: dict) -> None:
    try:
        _ensure_dirs()
        _write_atomic(STATE_FILE, json.dumps(state, default=str))
    except Exception:
        pass


def schedule_save() -> None:
    """Queue a debounced state save, coalescing bursts of mutations into one write."""
    global _save_task
    if _save_task is not None and not _save_task.done():
        return
    try:
        _save_task = asyncio.get_running_loop().create_task(_save_after_delay())
    except RuntimeError:
        pass  # no running loop (tests, CLI) — nothing to debounce against


async def _save_after_delay() -> None:
    from .state import STORE

    await asyncio.sleep(SAVE_DEBOUNCE_S)
    save_state(STORE.state_dict())


def flush() -> None:
    """Save immediately, cancelling any pending debounced write.

    Called on shutdown: a stop within the debounce window would otherwise drop
    the last second of work, which is exactly the moment a human is most likely
    to notice something missing.
    """
    global _save_task
    from .state import STORE

    if _save_task is not None and not _save_task.done():
        _save_task.cancel()
    _save_task = None
    save_state(STORE.state_dict())


def load_state() -> dict | None:
    """The workspace as of the last save, or None if nothing was persisted."""
    _resume_seq()
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def clear() -> None:
    """Drop the persisted workspace, leaving the history log (and arrays) intact."""
    save_state({"active_view": None, "views": [], "panels": {}, "comments": []})


# ---------- materialized data arrays ----------


def save_array(ref_id: str, values: list) -> None:
    """Persist a resolved $ref array, keyed by its content hash."""
    path = DATA_DIR / f"{ref_id}.json"
    if path.exists():
        return  # ref_id is a content hash, so an existing file is already correct
    try:
        _ensure_dirs()
        _write_atomic(path, json.dumps(values, default=str))
    except Exception:
        pass


def load_array(ref_id: str) -> list | None:
    """Read back an array a previous daemon materialized, or None if we never had it."""
    try:
        values = json.loads((DATA_DIR / f"{ref_id}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return values if isinstance(values, list) else None


def workspace_info() -> dict[str, Any]:
    """Where things are on disk — reported by server_status so the human can find them."""
    return {
        "dir": str(WORKSPACE_DIR),
        "state_file": str(STATE_FILE),
        "history_file": str(HISTORY_FILE),
        "history_entries": _history_seq,
    }
