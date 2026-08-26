"""Selective sharing of Codex hooks between the root home and profiles."""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

from .errors import HookConfigError
from .models import HookOption, HookSyncResult

HOOKS_FILENAME = "hooks.json"
PROFILE_STATE_FILENAME = ".codexalias.json"
SYNC_TYPES_KEY = "types"


@dataclass(frozen=True, slots=True)
class _SourceHook:
    key: str
    event: str
    matcher: Any
    hook: dict[str, Any]
    source: str = "root"


def hooks_path(home: Path) -> Path:
    return home / HOOKS_FILENAME


def profile_state_path(home: Path) -> Path:
    return home / PROFILE_STATE_FILENAME


def _read_json(path: Path, *, missing_ok: bool = False) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        if missing_ok:
            return {"hooks": {}}, False
        raise HookConfigError(f"hook file not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"failed to read hook file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HookConfigError(f"invalid hook file root: {path}")
    _validate_hooks_shape(value, path)
    return value, True


def _validate_hooks_shape(document: dict[str, Any], path: Path) -> None:
    hooks = document.get("hooks", {})
    if hooks is None:
        document["hooks"] = {}
        return
    if not isinstance(hooks, dict):
        raise HookConfigError(f"invalid hooks object in {path}")
    for event, rules in hooks.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            raise HookConfigError(f"invalid hook event {event!r} in {path}")
        for rule in rules:
            if not isinstance(rule, dict):
                raise HookConfigError(f"invalid hook rule for {event} in {path}")
            nested = rule.get("hooks", [])
            if not isinstance(nested, list):
                raise HookConfigError(f"invalid nested hooks for {event} in {path}")
            if any(not isinstance(hook, dict) for hook in nested):
                raise HookConfigError(f"invalid hook entry for {event} in {path}")


def _hook_key(event: str, rule_index: int, hook_index: int) -> str:
    return f"{event}:{rule_index}:{hook_index}"


def _source_hooks(
    document: dict[str, Any], *, key_prefix: str = "", source: str = "root",
    plugin_root: Path | None = None
) -> list[_SourceHook]:
    result: list[_SourceHook] = []
    for event, rules in document.get("hooks", {}).items():
        for rule_index, rule in enumerate(rules):
            matcher = rule.get("matcher")
            for hook_index, hook in enumerate(rule.get("hooks", [])):
                bound_hook = copy.deepcopy(hook)
                if plugin_root is not None:
                    bound_hook = _bind_plugin_root(bound_hook, plugin_root)
                result.append(
                    _SourceHook(
                        key=f"{key_prefix}{_hook_key(event, rule_index, hook_index)}",
                        event=event,
                        matcher=matcher,
                        hook=bound_hook,
                        source=source,
                    )
                )
    return result


def _bind_plugin_root(hook: dict[str, Any], plugin_root: Path) -> dict[str, Any]:
    """Make a plugin hook usable after it is copied out of plugin context."""
    command = hook.get("command")
    if not isinstance(command, str):
        return hook
    if "${PLUGIN_ROOT}" not in command and "${CLAUDE_PLUGIN_ROOT}" not in command:
        return hook
    root = shlex.quote(str(plugin_root))
    hook["command"] = (
        f"PLUGIN_ROOT={root}; export PLUGIN_ROOT; "
        f"CLAUDE_PLUGIN_ROOT={root}; export CLAUDE_PLUGIN_ROOT; {command}"
    )
    return hook


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HookConfigError(f"failed to read Codex config {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _plugin_root(source_home: Path, plugin_id: str) -> Path | None:
    plugin_name, separator, marketplace = plugin_id.partition("@")
    if not separator or not plugin_name or not marketplace:
        return None
    direct_candidates = (
        source_home / ".tmp" / "marketplaces" / marketplace / "plugins" / plugin_name,
        source_home / ".tmp" / "plugins" / "plugins" / plugin_name,
    )
    for candidate in direct_candidates:
        if (candidate / ".codex-plugin" / "plugin.json").is_file():
            return candidate

    cached = source_home / "plugins" / "cache" / marketplace / plugin_name
    if not cached.is_dir():
        return None
    versions = sorted(
        (candidate for candidate in cached.iterdir() if candidate.is_dir()),
        key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
        reverse=True,
    )
    for candidate in versions:
        if (candidate / ".codex-plugin" / "plugin.json").is_file():
            return candidate
    return None


def _plugin_hooks(source_home: Path) -> list[_SourceHook]:
    config = _read_toml(source_home / "config.toml")
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        return []

    result: list[_SourceHook] = []
    for plugin_id, settings in plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(settings, dict):
            continue
        if settings.get("enabled") is not True:
            continue
        root = _plugin_root(source_home, plugin_id)
        if root is None:
            continue
        try:
            manifest_value = json.loads(
                (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest_value, dict):
            continue
        manifest = manifest_value
        hooks_ref = manifest.get("hooks")
        if not isinstance(hooks_ref, str):
            continue
        hooks_file = (root / hooks_ref).resolve()
        if not hooks_file.is_file():
            continue
        try:
            document, _ = _read_json(hooks_file)
        except HookConfigError:
            continue
        result.extend(
            _source_hooks(
                document,
                key_prefix=f"plugin:{plugin_id}:",
                source=plugin_id,
                plugin_root=root,
            )
        )
    return result


def _all_source_hooks(source_home: Path) -> list[_SourceHook]:
    document, _ = _read_json(hooks_path(source_home), missing_ok=True)
    return _source_hooks(document) + _plugin_hooks(source_home)


def _hook_detail(hook: dict[str, Any]) -> tuple[str, str]:
    hook_type = str(hook.get("type", "command"))
    for field in ("command", "prompt"):
        value = hook.get(field)
        if isinstance(value, str):
            return hook_type, value
    return hook_type, json.dumps(hook, ensure_ascii=False, sort_keys=True)


def _matcher_label(value: Any) -> str:
    if value is None:
        return "*"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_state(path: Path) -> tuple[dict[str, Any], bool]:
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
    state, _ = _read_state(profile_state_path(target_home))
    return tuple(_sync_types_from_state(state))


def record_sync_type(target_home: Path, sync_type: str) -> None:
    """Record one migration type without recording a new selection payload."""
    sync_type = sync_type.strip()
    if not sync_type:
        raise HookConfigError("sync type must not be empty")
    state, _ = _read_state(profile_state_path(target_home))
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
        _write_json(profile_state_path(target_home), next_state, backup=False)


def remove_sync_type(target_home: Path, sync_type: str) -> None:
    """Remove one saved migration type without touching other state."""
    sync_type = sync_type.strip()
    if not sync_type:
        raise HookConfigError("sync type must not be empty")
    state, _ = _read_state(profile_state_path(target_home))
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
        _write_json(profile_state_path(target_home), next_state, backup=False)


def saved_skill_sync_options(target_home: Path) -> dict[str, Any] | None:
    """Return the persisted skill selector for a profile, if configured."""
    state, _ = _read_state(profile_state_path(target_home))
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
    state, _ = _read_state(profile_state_path(target_home))
    next_state = copy.deepcopy(state)
    sync = next_state.setdefault("sync", {})
    if not isinstance(sync, dict):
        sync = {}
        next_state["sync"] = sync
    # A saved legacy ``plugins`` entry means “copy the old all-in-one bundle”
    # and would defeat a newly selected skills allowlist. Saving granular skill
    # settings therefore replaces that legacy entry; plugin/rule/prompt syncs
    # can be enabled independently afterward.
    types = [value for value in _sync_types_from_state(state) if value != "plugins"]
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
        _write_json(profile_state_path(target_home), next_state, backup=False)


def _selected_keys(
    state: dict[str, Any], target_document: dict[str, Any], source: list[_SourceHook]
) -> set[str]:
    saved = _saved_hook_state(state)
    if saved is not None and isinstance(saved.get("selected"), list):
        return {str(value) for value in saved["selected"]}

    selected: set[str] = set()
    for source_hook in source:
        if _contains_hook(
            target_document,
            source_hook.event,
            source_hook.matcher,
            source_hook.hook,
        ):
            selected.add(source_hook.key)
    return selected


def list_options(source_home: Path, target_home: Path) -> list[HookOption]:
    """List root hooks with selection state from a target profile."""
    target_document, _ = _read_json(hooks_path(target_home), missing_ok=True)
    state, _ = _read_state(profile_state_path(target_home))
    source = _all_source_hooks(source_home)
    selected = _selected_keys(state, target_document, source)
    return [
        HookOption(
            key=item.key,
            event=item.event,
            matcher=_matcher_label(item.matcher),
            hook_type=_hook_detail(item.hook)[0],
            detail=_hook_detail(item.hook)[1],
            source=item.source,
            selected=item.key in selected,
        )
        for item in source
    ]


def sync_saved_hooks(source_home: Path, target_home: Path) -> HookSyncResult:
    """Apply the target profile's saved root-hook selection."""
    state, exists = _read_state(profile_state_path(target_home))
    saved = _saved_hook_state(state)
    if not exists or saved is None or not isinstance(saved.get("selected"), list):
        raise HookConfigError(
            f"profile has no saved hook settings: {target_home}; "
            "run 'codexalias hooks' first"
        )
    selected = {str(value) for value in saved["selected"]}
    return _apply_selection(source_home, target_home, selected, state)


def configure_hooks(
    source_home: Path, target_home: Path, selected: set[str]
) -> HookSyncResult:
    """Persist and apply a selected set of root hooks to a profile."""
    state, _ = _read_state(profile_state_path(target_home))
    return _apply_selection(source_home, target_home, selected, state)


def _apply_selection(
    source_home: Path,
    target_home: Path,
    selected: set[str],
    state: dict[str, Any],
) -> HookSyncResult:
    source_path = hooks_path(source_home)
    target_path = hooks_path(target_home)
    target_document, _ = _read_json(target_path, missing_ok=True)
    source = _all_source_hooks(source_home)
    source_by_key = {item.key: item for item in source}
    valid_selected = [item for item in source if item.key in selected]
    missing = tuple(sorted(selected - set(source_by_key)))

    original_target = copy.deepcopy(target_document)
    saved = _saved_hook_state(state) or {}
    applied = saved.get("applied", {})
    if not isinstance(applied, dict):
        applied = {}

    removed = 0
    retained_owned: set[str] = set()
    for key, snapshot in applied.items():
        if not isinstance(snapshot, dict) or not snapshot.get("owned", False):
            continue
        source_hook = source_by_key.get(key)
        if (
            key in selected
            and source_hook is not None
            and snapshot.get("event") == source_hook.event
            and snapshot.get("matcher") == source_hook.matcher
            and snapshot.get("hook") == source_hook.hook
        ):
            retained_owned.add(key)
            continue
        if _remove_snapshot(target_document, snapshot):
            removed += 1

    next_applied: dict[str, dict[str, Any]] = {}
    added = 0
    for item in valid_selected:
        already_present = _contains_hook(
            target_document, item.event, item.matcher, item.hook
        )
        if not already_present:
            _append_hook(target_document, item)
            added += 1
            owned = True
        else:
            owned = item.key in retained_owned
        next_applied[item.key] = {
            "event": item.event,
            "matcher": copy.deepcopy(item.matcher),
            "hook": copy.deepcopy(item.hook),
            "owned": owned,
        }

    selected_ordered = [item.key for item in source if item.key in selected]
    saved_state = {
        "source": "default",
        "selected": selected_ordered,
        "applied": next_applied,
    }
    next_state = copy.deepcopy(state)
    sync = next_state.setdefault("sync", {})
    if not isinstance(sync, dict):
        sync = {}
        next_state["sync"] = sync
    sync_types = _sync_types_from_state(state)
    if "hooks" not in sync_types:
        sync_types.append("hooks")
    sync[SYNC_TYPES_KEY] = sync_types
    sync["hooks"] = saved_state
    next_state.setdefault("version", 1)

    hook_changed = target_document != original_target
    state_changed = next_state != state
    backup_path: Path | None = None
    if hook_changed:
        target_home.mkdir(parents=True, exist_ok=True)
        backup_path = _write_json(target_path, target_document, backup=True)
    if state_changed:
        target_home.mkdir(parents=True, exist_ok=True)
        _write_json(profile_state_path(target_home), next_state, backup=False)

    return HookSyncResult(
        source_path=source_path,
        target_path=target_path,
        selected_count=len(selected_ordered),
        added=added,
        removed=removed,
        missing=missing,
        backup_path=backup_path,
        changed=hook_changed or state_changed,
    )


def _contains_hook(
    document: dict[str, Any], event: str, matcher: Any, hook: dict[str, Any]
) -> bool:
    for rule in document.get("hooks", {}).get(event, []):
        if rule.get("matcher") != matcher:
            continue
        if hook in rule.get("hooks", []):
            return True
    return False


def _append_hook(document: dict[str, Any], item: _SourceHook) -> None:
    hooks = document.setdefault("hooks", {})
    rules = hooks.setdefault(item.event, [])
    rule: dict[str, Any] = {"hooks": [copy.deepcopy(item.hook)]}
    if item.matcher is not None:
        rule["matcher"] = copy.deepcopy(item.matcher)
    rules.append(rule)


def _remove_snapshot(document: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    event = snapshot.get("event")
    matcher = snapshot.get("matcher")
    hook = snapshot.get("hook")
    if not isinstance(event, str) or not isinstance(hook, dict):
        return False
    rules = document.get("hooks", {}).get(event, [])
    for rule_index in range(len(rules) - 1, -1, -1):
        rule = rules[rule_index]
        if rule.get("matcher") != matcher:
            continue
        nested = rule.get("hooks", [])
        for hook_index in range(len(nested) - 1, -1, -1):
            if nested[hook_index] == hook:
                nested.pop(hook_index)
                if not nested:
                    rules.pop(rule_index)
                if not rules:
                    document["hooks"].pop(event, None)
                return True
    return False


def _next_backup(path: Path) -> Path:
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}.backup.{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _write_json(path: Path, value: dict[str, Any], *, backup: bool) -> Path | None:
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
