"""Resolve and classify Codex homes used by profile and session workflows."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable

from .config import Config
from .errors import HomeNotFoundError
from .models import HomeKind, HomeRef, Profile

# Reference tokens accepted anywhere a home/profile is expected.
REF_SOURCE = "@source"
REF_CURRENT = "@current"


def safe_resolve(path: Path) -> Path:
    """Resolve a path without failing when its final component is missing."""
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


class HomeResolver:
    """Resolve symbolic home references without performing filesystem writes."""

    def __init__(
        self,
        config: Config,
        profiles: Callable[[], Iterable[Profile]],
    ) -> None:
        self.config = config
        self._profiles = profiles

    def current_home(self) -> Path:
        """Return the home used by a bare ``codex`` invocation right now."""
        env_home = os.environ.get("CODEX_HOME")
        return Path(env_home) if env_home else self.config.source_home

    def default_source_home(self) -> Path:
        """Return the canonical ``~/.codex`` used by session import."""
        return Path(os.environ.get("HOME", str(Path.home()))) / ".codex"

    def describe_home(self, path: Path) -> HomeRef:
        """Classify a home relative to current/source/managed profiles."""
        resolved = self._resolve_existing(path)

        if resolved == safe_resolve(self.current_home()):
            return HomeRef(resolved, HomeKind.CURRENT)
        if resolved == safe_resolve(self.config.source_home):
            return HomeRef(resolved, HomeKind.SOURCE)
        for profile in self._profiles():
            if resolved == safe_resolve(profile.path):
                return HomeRef(resolved, HomeKind.PROFILE, profile.name)
        return HomeRef(resolved, HomeKind.OTHER)

    def resolve_home_ref(self, ref: str | None) -> HomeRef:
        """Resolve ``@source``, ``@current``, a profile name, or a path."""
        ref = ref or REF_CURRENT
        if ref == REF_SOURCE:
            return self.describe_home(self.config.source_home)
        if ref == REF_CURRENT:
            return self.describe_home(self.current_home())

        candidate = self.config.profile_path(ref)
        if candidate.is_dir():
            return self.describe_home(candidate)
        as_path = Path(ref).expanduser()
        if as_path.is_dir():
            return self.describe_home(as_path)
        raise HomeNotFoundError(f"unknown home/profile: {ref}")

    def candidate_source_homes(self, target_home: Path) -> list[HomeRef]:
        """Return source homes usable for migration into ``target_home``."""
        target = safe_resolve(target_home)
        source = safe_resolve(self.config.source_home)
        refs: list[HomeRef] = []
        if source != target:
            refs.append(self.describe_home(self.config.source_home))
        for profile in self._profiles():
            resolved = safe_resolve(profile.path)
            if resolved not in (target, source):
                refs.append(HomeRef(resolved, HomeKind.PROFILE, profile.name))
        return refs

    @staticmethod
    def _resolve_existing(path: Path) -> Path:
        if not path.exists():
            raise HomeNotFoundError(f"directory not found: {path}")
        return safe_resolve(path)
