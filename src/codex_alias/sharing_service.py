"""Session storage sharing between isolated Codex homes.

The service owns the destructive-looking details of sharing (backups, link
replacement, and SQLite sidecars).  Resolving a profile name and rendering the
result remain outside this module.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .config import Config
from .home_service import REF_SOURCE
from . import profile_state
from .models import HomeRef, LinkAction

_SHARED_DBS = ("state_5.sqlite", "logs_1.sqlite")
ProfileHome = Callable[..., Path]
HomeRefResolver = Callable[[str | None], HomeRef]


class SessionSharingService:
    """Link session artifacts from one Codex home into a profile home."""

    def __init__(
        self,
        config: Config,
        *,
        profile_home: ProfileHome,
        resolve_home_ref: HomeRefResolver,
    ) -> None:
        self.config = config
        self._profile_home = profile_home
        self._resolve_home_ref = resolve_home_ref

    def share_sessions(
        self, profile: str, source_ref: str = REF_SOURCE
    ) -> list[LinkAction]:
        """Share a named profile's session storage with ``source_ref``."""
        profile_path = self._profile_home(profile, must_exist=True)
        source_home = self._resolve_home_ref(source_ref).path
        actions = self.link_shared(profile_path, source_home)
        profile_state.record_sync_type(profile_path, "sessions_shared")
        return actions

    def link_shared(
        self, profile_path: Path, source_home: Path
    ) -> list[LinkAction]:
        """Link sessions, history, and known Codex databases.

        Existing real files/directories are moved to numbered backups before a
        link replaces them.  Existing links are replaced in place so repeated
        calls stay idempotent.
        """
        actions: list[LinkAction] = []
        sessions_link = profile_path / "sessions"
        source_sessions = source_home / "sessions"

        if sessions_link.exists() and not sessions_link.is_symlink():
            backup = self._backup(sessions_link)
            actions.append(LinkAction(f"Backed up existing sessions -> {backup}"))

        source_sessions.mkdir(parents=True, exist_ok=True)
        self._force_symlink(source_sessions, sessions_link)
        actions.append(LinkAction(f"Linked {sessions_link} -> {source_sessions}"))

        source_history = source_home / "history.jsonl"
        history_link = profile_path / "history.jsonl"
        if source_history.exists() or source_history.is_symlink():
            if history_link.exists() and not history_link.is_symlink():
                backup = self._backup(history_link)
                actions.append(LinkAction(f"Backed up existing history -> {backup}"))
            self._force_symlink(source_history, history_link)
            actions.append(LinkAction(f"Linked {history_link} -> {source_history}"))

        for db_name in _SHARED_DBS:
            source_db = source_home / db_name
            if not source_db.is_file():
                continue
            target_db = profile_path / db_name
            if target_db.exists() and not target_db.is_symlink():
                self._backup(target_db)
                for suffix in ("-wal", "-shm"):
                    sidecar = target_db.with_name(target_db.name + suffix)
                    sidecar.unlink(missing_ok=True)
                actions.append(LinkAction(f"Backed up existing {db_name}"))
            self._force_symlink(source_db, target_db)
            actions.append(LinkAction(f"Linked {target_db} -> {source_db}"))

        return actions

    @staticmethod
    def _force_symlink(source: Path, link: Path) -> None:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)

    @staticmethod
    def _backup(path: Path) -> Path:
        """Move ``path`` aside to a unique ``.backup.N`` name."""
        index = 1
        while True:
            backup = path.with_name(f"{path.name}.backup.{index}")
            if not backup.exists():
                break
            index += 1
        shutil.move(str(path), str(backup))
        return backup
