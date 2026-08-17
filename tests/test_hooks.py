from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from codex_alias import CodexAlias, HookConfigError
from codex_alias.cli import cli
from codex_alias import ui


def _write_hooks(home, document: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _root_hooks() -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|clear",
                    "hooks": [
                        {"type": "command", "command": "root-start"},
                    ],
                }
            ],
            "SessionEnd": [
                {
                    "hooks": [
                        {"type": "command", "command": "root-end"},
                    ]
                }
            ],
        }
    }


def _profile_hooks() -> dict:
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "profile-only"},
                    ]
                }
            ]
        }
    }


def _read_hooks(home) -> dict:
    return json.loads((home / "hooks.json").read_text(encoding="utf-8"))


def _write_agent_trace_plugin(home) -> None:
    plugin_root = (
        home / ".tmp" / "marketplaces" / "ai-minions-skills" / "plugins" / "agent-trace"
    )
    (plugin_root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_root / "hooks").mkdir(parents=True, exist_ok=True)
    (plugin_root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"hooks": "./hooks/codex-hooks.json"}), encoding="utf-8"
    )
    (plugin_root / "hooks" / "codex-hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup|resume",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash \"${PLUGIN_ROOT}/scripts/ship-transcript.sh\" session_start",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (home / "config.toml").write_text(
        '[plugins."agent-trace@ai-minions-skills"]\nenabled = true\n',
        encoding="utf-8",
    )


def test_profile_hook_selection_preserves_custom_hooks_and_persists_settings(
    mgr: CodexAlias,
) -> None:
    root = mgr.default_source_home()
    _write_hooks(root, _root_hooks())
    mgr.add_profile("work")
    target = mgr.config.profile_path("work")
    _write_hooks(target, _profile_hooks())

    options = mgr.profile_hook_options("work")
    assert [option.event for option in options] == ["SessionStart", "SessionEnd"]
    assert all(not option.selected for option in options)

    result = mgr.configure_profile_hooks("work", {options[0].key})

    assert result.added == 1
    assert result.removed == 0
    document = _read_hooks(target)
    assert document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "profile-only"
    assert document["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "root-start"
    assert "SessionEnd" not in document["hooks"]

    state = json.loads((target / ".codexalias.json").read_text(encoding="utf-8"))
    assert state["sync"]["hooks"]["selected"] == [options[0].key]
    assert state["sync"]["hooks"]["applied"][options[0].key]["owned"] is True


def test_enabled_codex_plugin_hooks_are_selectable_and_bound(mgr: CodexAlias) -> None:
    root = mgr.default_source_home()
    _write_hooks(root, _root_hooks())
    _write_agent_trace_plugin(root)
    mgr.add_profile("work")

    options = mgr.profile_hook_options("work")
    trace = next(option for option in options if option.source == "agent-trace@ai-minions-skills")
    assert trace.event == "SessionStart"
    assert "export PLUGIN_ROOT" in trace.detail
    assert "scripts/ship-transcript.sh" in trace.detail

    result = mgr.configure_profile_hooks("work", {trace.key})

    assert result.added == 1
    target = mgr.config.profile_path("work")
    command = _read_hooks(target)["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
    assert "PLUGIN_ROOT=" in command
    assert "scripts/ship-transcript.sh" in command


def test_profile_hook_sync_replaces_changed_root_hook(mgr: CodexAlias) -> None:
    root = mgr.default_source_home()
    _write_hooks(root, _root_hooks())
    mgr.add_profile("work")
    target = mgr.config.profile_path("work")
    options = mgr.profile_hook_options("work")
    mgr.configure_profile_hooks("work", {options[0].key})

    changed = _root_hooks()
    changed["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "root-start-v2"
    _write_hooks(root, changed)

    result = mgr.sync_profile_hooks("work")

    assert result.removed == 1
    assert result.added == 1
    document = _read_hooks(target)
    commands = [
        hook["command"]
        for rules in document["hooks"].values()
        for rule in rules
        for hook in rule["hooks"]
        if "command" in hook
    ]
    assert "root-start" not in commands
    assert "root-start-v2" in commands


def test_reapplying_the_same_hook_selection_is_idempotent(mgr: CodexAlias) -> None:
    root = mgr.default_source_home()
    _write_hooks(root, _root_hooks())
    mgr.add_profile("work")
    options = mgr.profile_hook_options("work")
    selected = {options[0].key}

    mgr.configure_profile_hooks("work", selected)
    result = mgr.configure_profile_hooks("work", selected)

    assert result.added == 0
    assert result.removed == 0
    assert result.changed is False
    assert result.backup_path is None


def test_deselecting_hooks_removes_only_owned_entries(mgr: CodexAlias) -> None:
    root = mgr.default_source_home()
    _write_hooks(root, _root_hooks())
    mgr.add_profile("work")
    target = mgr.config.profile_path("work")
    _write_hooks(target, _profile_hooks())
    options = mgr.profile_hook_options("work")

    mgr.configure_profile_hooks("work", {options[0].key})
    result = mgr.configure_profile_hooks("work", set())

    assert result.removed == 1
    document = _read_hooks(target)
    assert "SessionStart" not in document["hooks"]
    assert "UserPromptSubmit" in document["hooks"]


def test_sync_requires_saved_profile_settings(mgr: CodexAlias) -> None:
    root = mgr.default_source_home()
    _write_hooks(root, _root_hooks())
    mgr.add_profile("work")

    with pytest.raises(HookConfigError, match="no saved hook settings"):
        mgr.sync_profile_hooks("work")


def test_sync_command_applies_saved_hooks(tmp_path, monkeypatch) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    _write_hooks(source, _root_hooks())
    target.mkdir(parents=True)
    (target / ".codexalias.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sync": {"hooks": {"source": "default", "selected": ["SessionStart:0:0"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("SHELL", raising=False)

    result = CliRunner().invoke(
        cli,
        ["sync", "work"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert _read_hooks(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "root-start"


def test_hook_picker_reads_split_arrow_sequences_without_text_buffering(monkeypatch) -> None:
    class FakeStdin:
        def fileno(self) -> int:
            return 41

    chunks = iter((b"\x1b", b"[", b"B"))
    monkeypatch.setattr(ui.sys, "stdin", FakeStdin())
    monkeypatch.setattr(ui.os, "read", lambda fd, size: next(chunks))
    monkeypatch.setattr(ui.select, "select", lambda *args: ([41], [], []))

    assert ui._read_key() == "down"


def test_hook_picker_keeps_bare_escape_as_cancel(monkeypatch) -> None:
    class FakeStdin:
        def fileno(self) -> int:
            return 41

    monkeypatch.setattr(ui.sys, "stdin", FakeStdin())
    monkeypatch.setattr(ui.os, "read", lambda fd, size: b"\x1b")
    monkeypatch.setattr(ui.select, "select", lambda *args: ([], [], []))

    assert ui._read_key() == "cancel"
