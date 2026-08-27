"""Profile-home lifecycle operations.

``ProfileStore`` owns the filesystem contract for named profiles.  Launch
construction lives in :mod:`codex_alias.launcher`; session, hook, and sync
operations stay in their respective modules.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from .config import Config
from .errors import CodexAliasError, ProfileNotFoundError
from .launcher import ProfileLauncher
from .models import Profile, ProfileRemoveResult
from .validation import validate_name


class ProfileStore:
    """Create, enumerate, and remove profiles under one configured root."""

    def __init__(self, config: Config, launcher: ProfileLauncher | None = None) -> None:
        self.config = config
        self.launcher = launcher or ProfileLauncher(config)

    def list_profiles(self) -> list[Profile]:
        """Return profiles discovered directly under the configured root."""
        root = self.config.profile_root
        if not root.is_dir():
            return []
        return [
            Profile(
                name=path.name,
                path=path,
                sessions_shared=(path / "sessions").is_symlink(),
            )
            for path in sorted(path for path in root.iterdir() if path.is_dir())
        ]

    def profile_home(self, profile: str, *, must_exist: bool = False) -> Path:
        """Resolve a named profile home without creating it."""
        validate_name(profile, "profile")
        path = self.config.profile_path(profile)
        if must_exist and not path.is_dir():
            raise ProfileNotFoundError(f"profile not found: {path}")
        return path

    def add_profile(self, profile: str, command_name: str | None = None) -> Path:
        """Create a profile home and its wrapper command."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")

        profile_path = self.config.profile_path(profile)
        profile_path.mkdir(parents=True, exist_ok=True)
        self.config.bin_dir.mkdir(parents=True, exist_ok=True)

        target = self.config.wrapper_path(command_name)
        target.write_text(self.launcher.wrapper_script(profile), encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return target

    def remove_wrapper(
        self, profile: str, command_name: str | None = None
    ) -> tuple[Path, bool]:
        """Delete a generated wrapper while leaving profile data intact."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")
        target = self.config.wrapper_path(command_name)
        if target.exists():
            target.unlink()
            return target, True
        return target, False

    def remove_profile(
        self,
        profile: str,
        command_name: str | None = None,
        *,
        keep_data: bool = False,
        source_home: Path,
        current_home: Path,
    ) -> ProfileRemoveResult:
        """Remove a profile wrapper and, unless requested, its home."""
        validate_name(profile, "profile")
        command_name = command_name or f"codex-{profile}"
        validate_name(command_name, "command name")

        profile_path = self.config.profile_path(profile)
        if not keep_data:
            if not profile_path.is_dir():
                raise ProfileNotFoundError(f"profile not found: {profile_path}")
            resolved = self._safe_resolve(profile_path)
            if resolved == self._safe_resolve(source_home):
                raise CodexAliasError(
                    f"refusing to remove {profile_path}: it is the configured source home"
                )
            if resolved == self._safe_resolve(current_home):
                raise CodexAliasError(
                    f"refusing to remove {profile_path}: it is the current CODEX_HOME"
                )

        wrapper_path = self.config.wrapper_path(command_name)
        wrapper_removed = False
        if wrapper_path.exists():
            wrapper_path.unlink()
            wrapper_removed = True

        home_removed = False
        if not keep_data:
            home_removed = self._remove_home(profile_path)

        return ProfileRemoveResult(
            profile=profile,
            profile_path=profile_path,
            wrapper_path=wrapper_path,
            wrapper_removed=wrapper_removed,
            home_removed=home_removed,
        )

    def refresh_wrappers(self) -> list[Path]:
        """Regenerate default wrapper commands for existing profiles."""
        return [self.add_profile(profile.name) for profile in self.list_profiles()]

    def _remove_home(self, profile_path: Path) -> bool:
        """Delete a profile home after verifying it stays under the root."""
        root = self._safe_resolve(self.config.profile_root)
        if root not in self._safe_resolve(profile_path).parents:
            raise CodexAliasError(
                f"refusing to remove path outside profile root: {profile_path}"
            )
        if profile_path.is_symlink():
            profile_path.unlink()
            return True
        if not profile_path.is_dir():
            return False
        shutil.rmtree(profile_path)
        return True

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()
