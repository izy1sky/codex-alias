"""Session migration orchestration across isolated Codex homes.

The JSONL/SQLite implementation remains in :mod:`codex_alias.sessions`.  This
service supplies the cross-home policy: source discovery, target configuration,
provider identity, and the public operations used by the CLI facade.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from .config import Config
from .errors import (
    AmbiguousSessionError,
    HomeNotFoundError,
    SessionNotFoundError,
    SessionRepairError,
)
from .home_service import safe_resolve
from .models import (
    Profile,
    SessionCloneResult,
    SessionCopyResult,
    SessionFile,
    SessionFixResult,
)
from . import sessions as sessions_mod
from .session_mappings import SessionMappingContext


class SessionService:
    """Coordinate session operations without rendering or CLI concerns."""

    def __init__(
        self,
        config: Config,
        *,
        default_source_home: Callable[[], Path],
        profiles: Callable[[], Iterable[Profile]],
    ) -> None:
        self.config = config
        self._default_source_home = default_source_home
        self._profiles = profiles

    def list_sessions(self, home: Path) -> list[SessionFile]:
        return sessions_mod.list_session_files(home)

    def resolve_session(self, home: Path, query: str) -> SessionFile:
        return sessions_mod.resolve_session_file(home, query)

    def copy_session(
        self, src_home: Path, session: SessionFile, dst_home: Path
    ) -> SessionCopyResult:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_session_by_query(
        self, src_home: Path, query: str, dst_home: Path
    ) -> SessionCopyResult:
        session = sessions_mod.resolve_session_file(src_home, query)
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_all_sessions(
        self, src_home: Path, dst_home: Path
    ) -> list[SessionCopyResult]:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_all_sessions(src_home, dst_home)

    def import_session(self, query: str, dst_home: Path) -> SessionCopyResult:
        """Copy one session from the canonical ``~/.codex`` home."""
        source = self._default_source_home()
        if not source.is_dir():
            raise HomeNotFoundError(f"default source home not found: {source}")
        return self.copy_session_by_query(source, query, dst_home)

    def find_session(self, query: str) -> tuple[Path, SessionFile]:
        """Find a session across the default and managed profile homes."""
        homes = [self._default_source_home(), *(p.path for p in self._profiles())]
        seen: set[Path] = set()
        matches: list[tuple[Path, SessionFile]] = []
        for home in homes:
            root = safe_resolve(home / "sessions")
            if root in seen or not root.is_dir():
                continue
            seen.add(root)
            try:
                matches.append((home, sessions_mod.resolve_session_file(home, query)))
            except (HomeNotFoundError, SessionNotFoundError):
                continue
        if not matches:
            raise SessionNotFoundError(f"session not found: {query}")
        unique = {safe_resolve(item[1].path): item for item in matches}
        if len(unique) > 1:
            raise AmbiguousSessionError(
                query, [str(item[1].path) for item in unique.values()]
            )
        return next(iter(unique.values()))

    def clone_session_for_profile(
        self, query: str, target_home: Path, *, allow_lossy: bool = True
    ) -> SessionCloneResult:
        src_home, session = self.find_session(query)
        provider = sessions_mod.configured_model_provider_or_none(target_home)
        if provider is None:
            provider, _, _ = sessions_mod.inspect_session_source(session)
        if provider is None:
            raise SessionRepairError(
                f"session provider is missing in both {session.path} and "
                f"config: {target_home / 'config.toml'}"
            )
        model = sessions_mod.configured_model_or_none(target_home)
        mapping_context = self._session_mapping_context(
            session, src_home, target_home, provider, model
        )
        return sessions_mod.clone_session_for_profile(
            src_home,
            session,
            target_home,
            provider,
            model,
            mapping_context=mapping_context,
            allow_lossy=allow_lossy,
        )

    def configured_model_provider(self, home: Path) -> str:
        return sessions_mod.configured_model_provider(home)

    def configured_model(self, home: Path) -> str:
        return sessions_mod.configured_model(home)

    def fix_session_provider(
        self,
        home: Path,
        query: str,
        provider: str,
        *,
        model: str | None = None,
        from_provider: str | None = None,
        dry_run: bool = False,
        allow_lossy: bool = True,
    ) -> SessionFixResult:
        session = sessions_mod.resolve_session_file(home, query)
        mapping_context = self._session_mapping_context(
            session, home, home, provider, model
        )
        result = sessions_mod.fix_session_provider(
            session,
            provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
            mapping_context=mapping_context,
            allow_lossy=allow_lossy,
        )
        state_changed, state_backup_path = sessions_mod.fix_session_state_provider(
            home,
            session.session_id,
            provider,
            model=model,
            from_provider=from_provider,
            dry_run=dry_run,
        )
        return replace(
            result,
            state_changed=state_changed,
            state_backup_path=state_backup_path,
        )

    def _backend_identity(
        self, provider: str | None, preferred_home: Path
    ) -> tuple[str, str | None] | None:
        """Resolve a provider alias without assuming the alias is the backend."""
        if provider is None:
            return None
        homes = [
            preferred_home,
            self._default_source_home(),
            self.config.source_home,
            *(profile.path for profile in self._profiles()),
        ]
        identities: set[tuple[str, str | None]] = set()
        seen: set[Path] = set()
        for home in homes:
            resolved = safe_resolve(home)
            if resolved in seen:
                continue
            seen.add(resolved)
            identity = sessions_mod.configured_backend_identity(home, provider)
            if identity is not None:
                identities.add(identity)
        if len(identities) == 1:
            return next(iter(identities))
        # Ambiguous or absent definitions are unknown. Guessing here could make
        # us discard valid ciphertext, so mappings remain conservative.
        return None

    def _session_mapping_context(
        self,
        session: SessionFile,
        source_home: Path,
        target_home: Path,
        target_provider: str,
        target_model: str | None,
    ) -> SessionMappingContext:
        source_provider, source_model, source_cli_version = (
            sessions_mod.inspect_session_source(session)
        )
        if target_model is None:
            try:
                target_model = sessions_mod.configured_model_or_none(target_home)
            except SessionRepairError:
                target_model = None
        source_identity = self._backend_identity(source_provider, source_home)
        target_identity = sessions_mod.configured_backend_identity(
            target_home, target_provider
        )
        return SessionMappingContext(
            source_model=source_model,
            target_model=target_model,
            source_provider=source_provider,
            target_provider=target_provider,
            source_backend_fingerprint=(
                source_identity[0] if source_identity is not None else None
            ),
            target_backend_fingerprint=(
                target_identity[0] if target_identity is not None else None
            ),
            source_cli_version=source_cli_version,
            target_wire_api=(
                target_identity[1] if target_identity is not None else None
            ),
        )
