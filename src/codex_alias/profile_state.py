"""Persistence for per-profile codexalias state.

The state file is shared by hooks and the other profile synchronizers.  Keeping
its schema and atomic writer here prevents the hook implementation from also
becoming the owner of unrelated sync settings.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .errors import HookConfigError

PROFILE_STATE_FILENAME = ".codexalias.json"
SYNC_TYPES_KEY = "types"

# ``plugins`` was the historical all-in-one entry.  ``bundle`` is its explicit
# replacement.  Neither should remain alongside a granular selector because
# either one would copy every skill back on a later bare sync.
LEGACY_BUNDLE_TYPES = frozenset({"plugins", "bundle"})


def profile_state_path(home: Path) -> Path:
    return home / PROFILE_STATE_FILENAME


def read_state(path: Path) -> tuple[dict[str, Any], bool]:
    """Read a profile state document, returning ``({}, False)`` when absent."""
    if not path.is_file():
        return {}, False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"failed to read profile state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HookConfigError(f"invalid profile state root: {path}")
    return value, True


def _saved_hook_state(state: dict[str, Any]) -> dict[str, Any] | None:
    sync = state.get("sync")
    if not isinstance(sync, dict):
        return None
    hooks = sync.get("hooks")
    return hooks if isinstance(hooks, dict) else None


def _sync_types_from_state(state: dict[str, Any]) -> list[str]:
    sync = state.get("sync")
    if not isinstance(sync, dict):
        return []
    raw_types = sync.get(SYNC_TYPES_KEY)
    if isinstance(raw_types, list):
        return list(
            dict.fromkeys(
                value for value in raw_types if isinstance(value, str) and value
            )
        )
    # Profiles written before sync.types was introduced only had hook sync
    # state. Keep those profiles usable without requiring a manual migration.
    if _saved_hook_state(state) is not None:
        return ["hooks"]
    return []


def saved_sync_types(target_home: Path) -> tuple[str, ...]:
    """Return the ordered migration types recorded for a profile."""
    state, _ = read_state(profile_state_path(target_home))
    return tuple(_sync_types_from_state(state))


def record_sync_type(target_home: Path, sync_type: str) -> None:
    """Record one migration type without recording a new selection payload."""
    sync_type = sync_type.strip()
    if not sync_type:
        raise HookConfigError("sync type must not be empty")
    state, _ = read_state(profile_state_path(target_home))
    next_state = copy.deepcopy(state)
    sync = next_state.setdefault("sync", {})
    if not isinstance(sync, dict):
        sync = {}
        next_state["sync"] = sync
    types = _sync_types_from_state(state)
    if sync_type not in types:
        types.append(sync_type)
    sync[SYNC_TYPES_KEY] = types
    next_state.setdefault("version", 1)
    if next_state != state:
        target_home.mkdir(parents=True, exist_ok=True)
        write_json(profile_state_path(target_home), next_state, backup=False)


def remove_sync_type(target_home: Path, sync_type: str) -> None:
    """Remove one saved migration type without touching other state."""
    sync_type = sync_type.strip()
    if not sync_type:
        raise HookConfigError("sync type must not be empty")
    state, _ = read_state(profile_state_path(target_home))
    types = _sync_types_from_state(state)
    if sync_type not in types:
        return
    next_state = copy.deepcopy(state)
    sync = next_state.setdefault("sync", {})
    if not isinstance(sync, dict):
        sync = {}
        next_state["sync"] = sync
    sync[SYNC_TYPES_KEY] = [value for value in types if value != sync_type]
    next_state.setdefault("version", 1)
    if next_state != state:
        target_home.mkdir(parents=True, exist_ok=True)
        write_json(profile_state_path(target_home), next_state, backup=False)


def saved_skill_sync_options(target_home: Path) -> dict[str, Any] | None:
    """Return the persisted skill selector for a profile, if configured."""
    state, _ = read_state(profile_state_path(target_home))
    sync = state.get("sync")
    if not isinstance(sync, dict):
        return None
    skills = sync.get("skills")
    if not isinstance(skills, dict):
        return None
    includes = skills.get("include", [])
    excludes = skills.get("exclude", [])
    if not isinstance(includes, list) or not isinstance(excludes, list):
        return None
    return {
        "include": tuple(value for value in includes if isinstance(value, str)),
        "exclude": tuple(value for value in excludes if isinstance(value, str)),
        "include_system": skills.get("include_system") is True,
        "prune": skills.get("prune") is True,
    }


def record_skill_sync_options(
    target_home: Path,
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    include_system: bool = False,
    prune: bool = False,
) -> None:
    """Persist a skill selector and enable the profile's ``skills`` sync."""
    state, _ = read_state(profile_state_path(target_home))
    next_state = copy.deepcopy(state)
    sync = next_state.setdefault("sync", {})
    if not isinstance(sync, dict):
        sync = {}
        next_state["sync"] = sync
    # A saved all-in-one entry would defeat a newly selected skills allowlist.
    # Drop both the historical name and the explicit replacement.
    types = [
        value
        for value in _sync_types_from_state(state)
        if value not in LEGACY_BUNDLE_TYPES
    ]
    if "skills" not in types:
        types.append("skills")
    sync[SYNC_TYPES_KEY] = types
    sync["skills"] = {
        "include": list(dict.fromkeys(include)),
        "exclude": list(dict.fromkeys(exclude)),
        "include_system": include_system,
        "prune": prune,
    }
    next_state.setdefault("version", 1)
    if next_state != state:
        target_home.mkdir(parents=True, exist_ok=True)
        write_json(profile_state_path(target_home), next_state, backup=False)


def write_json(path: Path, value: dict[str, Any], *, backup: bool) -> Path | None:
    """Atomically write JSON, optionally preserving a numbered backup."""
    backup_path: Path | None = None
    if backup and (path.exists() or path.is_symlink()):
        backup_path = _next_backup(path)
        shutil.copy2(path, backup_path)

    mode = 0o600
    if path.exists() or path.is_symlink():
        mode = path.stat().st_mode & 0o777
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(value, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except (OSError, TypeError, ValueError) as exc:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        raise HookConfigError(f"failed to write {path}: {exc}") from exc
    return backup_path


def _next_backup(path: Path) -> Path:
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.backup.{index}")
        if not candidate.exists():
            return candidate
        index += 1
