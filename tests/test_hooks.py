from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from codex_alias import CodexAlias, HookConfigError, HookOption
import codex_alias.cli as cli_module
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
    root = mgr.config.source_home
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
    assert state["sync"]["types"] == ["hooks"]
    assert state["sync"]["hooks"]["selected"] == [options[0].key]
    assert state["sync"]["hooks"]["applied"][options[0].key]["owned"] is True


def test_enabled_codex_plugin_hooks_are_selectable_and_bound(mgr: CodexAlias) -> None:
    root = mgr.config.source_home
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


def test_plugin_hooks_are_selectable_without_root_hooks_file(mgr: CodexAlias) -> None:
    root = mgr.config.source_home
    _write_agent_trace_plugin(root)
    mgr.add_profile("work")

    options = mgr.profile_hook_options("work")

    assert len(options) == 1
    assert options[0].source == "agent-trace@ai-minions-skills"


def test_profile_hook_sync_replaces_changed_root_hook(mgr: CodexAlias) -> None:
    root = mgr.config.source_home
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
    root = mgr.config.source_home
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
    root = mgr.config.source_home
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
    root = mgr.config.source_home
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
        ["sync", "--yes", "work"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert _read_hooks(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "root-start"


def test_sync_command_runs_recorded_types_in_order(tmp_path, monkeypatch) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    target.mkdir(parents=True)
    _write_hooks(source, _root_hooks())

    from codex_alias import hooks as hooks_module

    hooks_module.record_sync_type(target, "config")
    hooks_module.record_sync_type(target, "hooks")
    calls: list[str] = []

    def migration(name: str):
        def run(_mgr, _profile_path):
            calls.append(name)

        return run

    monkeypatch.setattr(
        cli_module,
        "_SYNC_MIGRATIONS",
        {"config": migration("config"), "hooks": migration("hooks")},
    )

    result = CliRunner().invoke(
        cli,
        ["sync", "--yes", "work"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert calls == ["config", "hooks"]


def test_sync_config_requires_confirmation_without_yes(tmp_path, monkeypatch) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    target.mkdir(parents=True)

    from codex_alias import hooks as hooks_module

    hooks_module.record_sync_type(target, "config")
    result = CliRunner().invoke(
        cli,
        ["sync", "work"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code != 0
    assert "pass --yes" in result.output


def test_sync_instructions_option_mirrors_files_and_records_type(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "AGENTS.md").write_text("root instructions\n", encoding="utf-8")
    (target / "AGENTS.md").write_text("old instructions\n", encoding="utf-8")
    (target / "AGENTS.override.md").write_text("stale override\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["sync", "--instructions", "--yes", "work"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "root instructions\n"
    assert not (target / "AGENTS.override.md").exists()
    state = json.loads((target / ".codexalias.json").read_text(encoding="utf-8"))
    assert state["sync"]["types"] == ["instructions"]


def test_sync_all_profiles_runs_explicit_type_once_from_selected_source(
    tmp_path,
) -> None:
    source = tmp_path / "root-codex"
    configured_source = tmp_path / "wrong-source"
    profile_root = tmp_path / "profiles"
    (source / "skills" / "shared").mkdir(parents=True)
    (source / "skills" / "shared" / "SKILL.md").write_text(
        "shared root skill\n", encoding="utf-8"
    )
    configured_source.mkdir()
    for name in ("alpha", "beta"):
        (profile_root / name).mkdir(parents=True)

    result = CliRunner().invoke(
        cli,
        [
            "sync",
            "--all",
            "--type",
            "bundle",
            "--source",
            str(source),
            "--yes",
        ],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(configured_source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    for name in ("alpha", "beta"):
        target = profile_root / name
        assert (target / "skills" / "shared" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "shared root skill\n"
        assert not (target / ".codexalias.json").exists()
        assert f"Syncing bundle for profile '{name}'" in result.output


def test_sync_explicit_types_are_distinct_and_keep_requested_order(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    (source / "skills" / "shared").mkdir(parents=True)
    (source / "skills" / "shared" / "SKILL.md").write_text(
        "skill\n", encoding="utf-8"
    )
    (source / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    target.mkdir(parents=True)

    result = CliRunner().invoke(
        cli,
        [
            "sync",
            "work",
            "--type",
            "instructions",
            "--type",
            "plugins",
            "--yes",
        ],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert result.output.index("Syncing instructions") < result.output.index(
        "Syncing plugins"
    )
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "instructions\n"
    assert not (target / "skills").exists()
    assert not (target / ".codexalias.json").exists()


def test_saved_legacy_plugins_type_still_syncs_the_full_bundle(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    (source / "skills" / "one").mkdir(parents=True)
    (source / "plugins" / "cache").mkdir(parents=True)
    (source / "rules").mkdir(parents=True)
    (source / "prompts").mkdir(parents=True)
    target.mkdir(parents=True)

    from codex_alias import hooks as hooks_module

    hooks_module.record_sync_type(target, "plugins")
    result = CliRunner().invoke(
        cli,
        ["sync", "work", "--yes"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert (target / "skills" / "one").is_dir()
    assert (target / "plugins" / "cache").is_dir()
    assert (target / "rules").is_dir()
    assert (target / "prompts").is_dir()


def test_sync_rejects_profile_with_all_profiles(tmp_path) -> None:
    result = CliRunner().invoke(cli, ["sync", "work", "--all"])

    assert result.exit_code != 0
    assert "cannot be used together" in result.output


def test_sync_lists_independent_types() -> None:
    result = CliRunner().invoke(cli, ["sync", "--list-types"])

    assert result.exit_code == 0, result.output
    for name in (
        "skills",
        "plugins",
        "agents",
        "mcp",
        "rules",
        "prompts",
        "instructions",
        "config",
        "hooks",
        "sessions_shared",
        "sessions_migrate",
    ):
        assert name in result.output


def test_add_bootstrap_offers_and_records_instruction_sync(mgr, monkeypatch) -> None:
    class FakeTty:
        @staticmethod
        def isatty() -> bool:
            return True

    source = mgr.config.source_home
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("shared instructions\n", encoding="utf-8")
    mgr.add_profile("work")
    answers = iter((False, True, False, False, False))
    prompts: list[str] = []

    def confirm(prompt: str, *, default: bool = False) -> bool:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(cli_module.sys, "stdin", FakeTty())
    monkeypatch.setattr(cli_module.ui, "confirm", confirm)
    cli_module._bootstrap_profile(mgr, mgr.config.profile_path("work"))

    target = mgr.config.profile_path("work")
    assert "Copy global instructions" in prompts[1]
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == (
        "shared instructions\n"
    )
    state = json.loads((target / ".codexalias.json").read_text(encoding="utf-8"))
    assert state["sync"]["types"] == ["instructions"]


def test_saved_instruction_sync_copies_override(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "AGENTS.md").write_text("base\n", encoding="utf-8")
    (source / "AGENTS.override.md").write_text("temporary override\n", encoding="utf-8")

    from codex_alias import hooks as hooks_module

    hooks_module.record_sync_type(target, "instructions")
    result = CliRunner().invoke(
        cli,
        ["sync", "--yes", "work"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "base\n"
    assert (target / "AGENTS.override.md").read_text(encoding="utf-8") == (
        "temporary override\n"
    )


def test_instruction_sync_skips_same_source_and_target(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    agents.write_text("keep me\n", encoding="utf-8")
    messages: list[str] = []
    monkeypatch.setattr(cli_module.ui, "info", messages.append)

    cli_module._copy_instruction_files(home, home)

    assert agents.read_text(encoding="utf-8") == "keep me\n"
    assert messages == [f"Instruction source and target are the same, skipped: {home}"]


def test_plugin_sync_includes_rules_and_legacy_prompts(tmp_path, monkeypatch) -> None:
    source = tmp_path / ".codex"
    target = tmp_path / "profile"
    (source / "rules").mkdir(parents=True)
    (source / "rules" / "default.rules").write_text("rule\n", encoding="utf-8")
    (source / "prompts").mkdir()
    (source / "prompts" / "review.md").write_text("prompt\n", encoding="utf-8")
    monkeypatch.setattr(cli_module.ui, "success", lambda _message: None)

    cli_module._copy_plugin_dirs(source, target)

    assert (target / "rules" / "default.rules").read_text(encoding="utf-8") == "rule\n"
    assert (target / "prompts" / "review.md").read_text(encoding="utf-8") == "prompt\n"


def test_skills_sync_supports_allowlist_and_excludes_system_by_default(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    for name in ("keep", "skip", ".system"):
        (source / "skills" / name).mkdir(parents=True)
        (source / "skills" / name / "SKILL.md").write_text(
            f"{name}\n", encoding="utf-8"
        )
    target.mkdir(parents=True)

    result = CliRunner().invoke(
        cli,
        ["sync", "work", "--type", "skills", "--skill", "keep", "--yes"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert (target / "skills" / "keep" / "SKILL.md").is_file()
    assert not (target / "skills" / "skip").exists()
    assert not (target / "skills" / ".system").exists()


def test_skills_sync_can_persist_selector_and_prune_stale_user_skills(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    for name in ("keep", "stale", ".system"):
        (source / "skills" / name).mkdir(parents=True)
        (source / "skills" / name / "SKILL.md").write_text(
            f"{name}\n", encoding="utf-8"
        )
    target.mkdir(parents=True)
    (target / "skills" / "stale").mkdir(parents=True)
    (target / "skills" / "stale" / "old.txt").write_text("old\n", encoding="utf-8")
    (target / "skills" / ".system").mkdir(parents=True)
    from codex_alias import hooks as hooks_module

    hooks_module.record_sync_type(target, "plugins")

    first = CliRunner().invoke(
        cli,
        [
            "sync",
            "work",
            "--type",
            "skills",
            "--skill",
            "keep",
            "--save",
            "--yes",
        ],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )
    assert first.exit_code == 0, first.output

    state = json.loads((target / ".codexalias.json").read_text(encoding="utf-8"))
    assert state["sync"]["types"] == ["skills"]
    assert state["sync"]["skills"]["include"] == ["keep"]
    assert (target / "skills" / "keep" / "SKILL.md").is_file()

    second = CliRunner().invoke(
        cli,
        ["sync", "work", "--prune-skills", "--yes"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_SOURCE_HOME": str(source),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )
    assert second.exit_code == 0, second.output
    assert not (target / "skills" / "stale").exists()
    assert (target / "skills" / ".system").is_dir()


def test_skills_listing_and_plugin_only_sync_are_separate(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    (source / "skills" / "one").mkdir(parents=True)
    (source / "skills" / ".system").mkdir(parents=True)
    (source / "plugins" / "cache").mkdir(parents=True)
    target.mkdir(parents=True)
    from codex_alias import hooks as hooks_module

    hooks_module.record_sync_type(target, "plugins")

    env = {
        "HOME": str(tmp_path),
        "CODEXALIAS_SOURCE_HOME": str(source),
        "CODEXALIAS_PROFILE_ROOT": str(profile_root),
    }
    listed = CliRunner().invoke(cli, ["sync", "--list-skills"], env=env)
    assert listed.exit_code == 0, listed.output
    assert "one" in listed.output
    assert ".system" not in listed.output

    listed_system = CliRunner().invoke(
        cli, ["sync", "--list-skills", "--include-system-skills"], env=env
    )
    assert listed_system.exit_code == 0, listed_system.output
    assert ".system" in listed_system.output

    synced = CliRunner().invoke(
        cli,
        ["sync", "work", "--type", "plugins", "--save", "--yes"],
        env=env,
    )
    assert synced.exit_code == 0, synced.output
    assert (target / "plugins" / "cache").is_dir()
    assert not (target / "skills").exists()
    state = json.loads((target / ".codexalias.json").read_text(encoding="utf-8"))
    assert state["sync"]["types"] == ["plugins-only"]


def test_skill_and_profile_lists_have_json_modes(tmp_path) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    (source / "skills" / "one").mkdir(parents=True)
    target.mkdir(parents=True)
    env = {
        "HOME": str(tmp_path),
        "CODEXALIAS_SOURCE_HOME": str(source),
        "CODEXALIAS_PROFILE_ROOT": str(profile_root),
    }

    skills_result = CliRunner().invoke(
        cli, ["sync", "--list-skills", "--json"], env=env
    )
    assert skills_result.exit_code == 0, skills_result.output
    assert json.loads(skills_result.output) == ["one"]

    types_result = CliRunner().invoke(
        cli, ["sync", "--list-types", "--json"], env=env
    )
    assert types_result.exit_code == 0, types_result.output
    assert {item["name"] for item in json.loads(types_result.output)} >= {
        "skills",
        "plugins",
        "rules",
    }

    profiles_result = CliRunner().invoke(cli, ["list", "--json"], env=env)
    assert profiles_result.exit_code == 0, profiles_result.output
    assert json.loads(profiles_result.output)[0]["name"] == "work"


def test_select_skills_table_persists_the_selected_allowlist(tmp_path, monkeypatch) -> None:
    source = tmp_path / ".codex"
    profile_root = tmp_path / "profiles"
    target = profile_root / "work"
    for name in ("one", "two"):
        (source / "skills" / name).mkdir(parents=True)
        (source / "skills" / name / "SKILL.md").write_text(name, encoding="utf-8")
    target.mkdir(parents=True)
    (target / "skills" / "old").mkdir(parents=True)
    (target / "skills" / "old" / "SKILL.md").write_text("old", encoding="utf-8")
    (target / "skills" / ".system").mkdir(parents=True)
    (target / "skills" / "migration-manifest.json").write_text(
        "{}", encoding="utf-8"
    )

    class FakeTty:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli_module.sys, "stdin", FakeTty())
    monkeypatch.setattr(cli_module.ui, "select_skills", lambda *args: {"two"})
    result = CliRunner().invoke(
        cli,
        ["sync", "work", "--select-skills", "--source", str(source), "--yes"],
        env={
            "HOME": str(tmp_path),
            "CODEXALIAS_PROFILE_ROOT": str(profile_root),
        },
    )

    assert result.exit_code == 0, result.output
    assert (target / "skills" / "two" / "SKILL.md").is_file()
    assert not (target / "skills" / "one").exists()
    assert not (target / "skills" / "old").exists()
    assert (target / "skills" / ".system").is_dir()
    assert (target / "skills" / "migration-manifest.json").is_file()
    state = json.loads((target / ".codexalias.json").read_text(encoding="utf-8"))
    assert state["sync"]["skills"]["include"] == ["two"]
    assert state["sync"]["skills"]["prune"] is True


def test_record_sync_type_is_idempotent_and_preserves_order(mgr: CodexAlias) -> None:
    mgr.add_profile("work")

    mgr.record_profile_sync_type("work", "plugins")
    mgr.record_profile_sync_type("work", "hooks")
    mgr.record_profile_sync_type("work", "plugins")

    state = json.loads(
        (mgr.config.profile_path("work") / ".codexalias.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["sync"]["types"] == ["plugins", "hooks"]


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


def test_hook_picker_keeps_checkbox_visible_on_narrow_terminal(monkeypatch) -> None:
    options = [
        HookOption(
            key="selected",
            event="SessionStart",
            matcher="startup|resume",
            hook_type="command",
            detail="PLUGIN_ROOT=/a/very/long/plugin/path/scripts/hook.sh",
            source="agent-trace@ai-minions-skills",
            selected=True,
        ),
        HookOption(
            key="unselected",
            event="UserPromptSubmit",
            matcher="*",
            hook_type="command",
            detail="bash /a/very/long/path/hook.sh",
            selected=False,
        ),
    ]
    rendered_console = Console(width=60, record=True, force_terminal=False)
    monkeypatch.setattr(ui, "console", rendered_console)

    ui._render_hook_picker(
        "work",
        Path("/tmp/root/hooks.json"),
        options,
        {"selected"},
        0,
    )

    rendered = rendered_console.export_text(styles=False)
    assert "> [x]" in rendered
    assert "  [ ]" in rendered


def test_skill_picker_renders_selection_column_on_narrow_terminal(monkeypatch) -> None:
    rendered_console = Console(width=42, record=True, force_terminal=False)
    monkeypatch.setattr(ui, "console", rendered_console)

    ui._render_skill_picker(
        "work",
        Path("/tmp/root/skills"),
        ["review-mr", "domain-modeling"],
        {"review-mr"},
        0,
    )

    rendered = rendered_console.export_text(styles=False)
    assert "> [x]" in rendered
    assert "domain-modeling" in rendered
