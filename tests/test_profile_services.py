from __future__ import annotations

from codex_alias.launcher import ProfileLauncher
from codex_alias.profile_service import ProfileStore


def test_profile_store_owns_home_and_wrapper_lifecycle(config) -> None:
    store = ProfileStore(config)

    wrapper = store.add_profile("work")

    assert wrapper == config.bin_dir / "codex-work"
    assert config.profile_path("work").is_dir()
    assert store.list_profiles()[0].name == "work"

    result = store.remove_profile(
        "work",
        source_home=config.source_home,
        current_home=config.source_home,
        keep_data=True,
    )

    assert result.wrapper_removed is True
    assert result.home_removed is False
    assert config.profile_path("work").is_dir()


def test_profile_launcher_is_independent_from_profile_store(config, monkeypatch) -> None:
    launcher = ProfileLauncher(config)
    monkeypatch.delenv("SHELL", raising=False)

    argv, env = launcher.run_argv("work", ["--version"])

    assert argv == ["codex", "--version"]
    assert env["CODEX_HOME"] == str(config.profile_path("work"))

