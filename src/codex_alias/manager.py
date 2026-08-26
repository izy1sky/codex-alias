"""High-level facade over profiles, homes, wrappers, and sessions.

``CodexAlias`` is the single object CLIs and other tools construct. Every method
is UI-free: it performs filesystem work and returns value objects or raises a
:class:`~codex_alias.errors.CodexAliasError`. Interactive selection/prompting lives
in the caller.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

from .config import Config
from .errors import (
    AmbiguousSessionError,
    CodexAliasError,
    HomeNotFoundError,
    SessionNotFoundError,
    SessionRepairError,
)
from .models import (
    DoctorReport,
    HomeKind,
    HomeRef,
    HookOption,
    HookSyncResult,
    LinkAction,
    Profile,
    ProfileRemoveResult,
    SessionCopyResult,
    SessionCloneResult,
    SessionFile,
    SessionFixResult,
)
from . import hooks as hooks_mod
from . import profile_state
from . import sessions as sessions_mod
from .launcher import ProfileLauncher
from .profile_service import ProfileStore
from .session_mappings import SessionMappingContext
from .validation import validate_name
_SHARED_DBS = ("state_5.sqlite", "logs_1.sqlite")

# Reference tokens accepted anywhere a home/profile is expected.
REF_SOURCE = "@source"
REF_CURRENT = "@current"


class CodexAlias:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.launcher = ProfileLauncher(config)
        self.profile_store = ProfileStore(config, self.launcher)

    # ------------------------------------------------------------------ homes

    def current_home(self) -> Path:
        """The home a bare ``codex`` invocation would use right now."""
        env_home = os.environ.get("CODEX_HOME")
        return Path(env_home) if env_home else self.config.source_home

    def default_source_home(self) -> Path:
        """The canonical ``~/.codex`` used by :meth:`import_session`."""
        return Path(os.environ.get("HOME", str(Path.home()))) / ".codex"

    def describe_home(self, path: Path) -> HomeRef:
        """Classify a home path relative to current/source/profiles."""
        resolved = self._resolve_existing(path)

        if resolved == self._safe_resolve(self.current_home()):
            return HomeRef(resolved, HomeKind.CURRENT)
        if resolved == self._safe_resolve(self.config.source_home):
            return HomeRef(resolved, HomeKind.SOURCE)
        for profile in self.list_profiles():
            if resolved == self._safe_resolve(profile.path):
                return HomeRef(resolved, HomeKind.PROFILE, profile.name)
        return HomeRef(resolved, HomeKind.OTHER)

    def resolve_home_ref(self, ref: str | None) -> HomeRef:
        """Turn ``@source`` / ``@current`` / profile name / path into a home.

        Defaults to the current home when ``ref`` is empty.
        """
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

    # --------------------------------------------------------------- profiles

    def list_profiles(self) -> list[Profile]:
        return self.profile_store.list_profiles()

    def profile_home(self, profile: str, *, must_exist: bool = False) -> Path:
        return self.profile_store.profile_home(profile, must_exist=must_exist)

    def add_profile(self, profile: str, command_name: str | None = None) -> Path:
        """Create a profile home and its wrapper command; return wrapper path."""
        return self.profile_store.add_profile(profile, command_name)

    def root_hooks_path(self) -> Path:
        """Return the hooks file belonging to the configured source home."""
        return hooks_mod.hooks_path(self.config.source_home)

    def profile_hook_options(self, profile: str) -> list[HookOption]:
        """List root hooks and their saved selection for PROFILE."""
        profile_path = self.profile_home(profile, must_exist=True)
        return hooks_mod.list_options(self.config.source_home, profile_path)

    def configure_profile_hooks(
        self, profile: str, selected: set[str]
    ) -> HookSyncResult:
        """Apply and remember selected root hooks for PROFILE."""
        profile_path = self.profile_home(profile, must_exist=True)
        return hooks_mod.configure_hooks(
            self.config.source_home, profile_path, selected
        )

    def record_profile_sync_type(self, profile: str, sync_type: str) -> None:
        """Remember that PROFILE should run one migration type during sync."""
        profile_path = self.profile_home(profile, must_exist=True)
        profile_state.record_sync_type(profile_path, sync_type)

    def remove_profile_sync_type(self, profile: str, sync_type: str) -> None:
        """Forget one saved migration type for PROFILE."""
        profile_path = self.profile_home(profile, must_exist=True)
        profile_state.remove_sync_type(profile_path, sync_type)

    def profile_sync_types(self, profile: str) -> tuple[str, ...]:
        """Return PROFILE's ordered migration types, if any were recorded."""
        profile_path = self.profile_home(profile, must_exist=True)
        return profile_state.saved_sync_types(profile_path)

    def profile_skill_sync_options(self, profile: str) -> dict[str, object] | None:
        """Return PROFILE's persisted skill selector, if one exists."""
        profile_path = self.profile_home(profile, must_exist=True)
        return profile_state.saved_skill_sync_options(profile_path)

    def record_profile_skill_sync_options(
        self,
        profile: str,
        *,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        include_system: bool = False,
        prune: bool = False,
    ) -> None:
        """Persist PROFILE's skill selector and enable skill synchronization."""
        profile_path = self.profile_home(profile, must_exist=True)
        profile_state.record_skill_sync_options(
            profile_path,
            include=include,
            exclude=exclude,
            include_system=include_system,
            prune=prune,
        )

    def sync_profile_hooks(self, profile: str) -> HookSyncResult:
        """Reapply PROFILE's saved root-hook selection."""
        profile_path = self.profile_home(profile, must_exist=True)
        return hooks_mod.sync_saved_hooks(self.config.source_home, profile_path)

    def remove_wrapper(self, profile: str, command_name: str | None = None) -> tuple[Path, bool]:
        """Delete a wrapper command; profile data is left intact."""
        return self.profile_store.remove_wrapper(profile, command_name)

    def remove_profile(
        self,
        profile: str,
        command_name: str | None = None,
        *,
        keep_data: bool = False,
    ) -> ProfileRemoveResult:
        """Remove a profile: its wrapper command and, unless ``keep_data``, its home.

        Deleting a home is refused when it is the configured source home or the
        current ``CODEX_HOME``, since removing either would break the tool.
        """
        return self.profile_store.remove_profile(
            profile,
            command_name,
            keep_data=keep_data,
            source_home=self.config.source_home,
            current_home=self.current_home(),
        )

    def run_argv(self, profile: str, args: list[str]) -> tuple[list[str], dict[str, str]]:
        """Build the argv and environment to exec ``codex`` under ``profile``.

        Returns without executing so the caller controls process replacement.
        """
        return self.launcher.run_argv(profile, args)

    def resume_argv(
        self, home: Path, session_id: str
    ) -> tuple[list[str], dict[str, str]]:
        """Build a resume invocation through the configured Codex wrapper."""
        return self.launcher.resume_argv(home, session_id)

    # Kept as compatibility shims for callers that used the old internals.
    def _codex_argv(self, args: list[str]) -> list[str]:
        return self.launcher.codex_argv(args)

    def _wrapper_script(self, profile: str) -> str:
        return self.launcher.wrapper_script(profile)

    def refresh_wrappers(self) -> list[Path]:
        """Regenerate default wrapper commands for every existing profile."""
        return self.profile_store.refresh_wrappers()

    # --------------------------------------------------------------- sessions

    def list_sessions(self, home: Path) -> list[SessionFile]:
        return sessions_mod.list_session_files(home)

    def resolve_session(self, home: Path, query: str) -> SessionFile:
        return sessions_mod.resolve_session_file(home, query)

    def copy_session(self, src_home: Path, session: SessionFile, dst_home: Path) -> SessionCopyResult:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_session_by_query(self, src_home: Path, query: str, dst_home: Path) -> SessionCopyResult:
        session = sessions_mod.resolve_session_file(src_home, query)
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_session(src_home, session, dst_home)

    def copy_all_sessions(self, src_home: Path, dst_home: Path) -> list[SessionCopyResult]:
        dst_home.mkdir(parents=True, exist_ok=True)
        return sessions_mod.copy_all_sessions(src_home, dst_home)

    def import_session(self, query: str, dst_home: Path) -> SessionCopyResult:
        """Copy one session from the default ``~/.codex`` into ``dst_home``."""
        source = self.default_source_home()
        if not source.is_dir():
            raise HomeNotFoundError(f"default source home not found: {source}")
        return self.copy_session_by_query(source, query, dst_home)

    def find_session(self, query: str) -> tuple[Path, SessionFile]:
        """Find a session across default and profile homes, de-duplicating links."""
        homes = [self.default_source_home(), *(p.path for p in self.list_profiles())]
        seen: set[Path] = set()
        matches: list[tuple[Path, SessionFile]] = []
        for home in homes:
            root = self._safe_resolve(home / "sessions")
            if root in seen or not root.is_dir():
                continue
            seen.add(root)
            try:
                matches.append((home, sessions_mod.resolve_session_file(home, query)))
            except (HomeNotFoundError, SessionNotFoundError):
                continue
        if not matches:
            raise SessionNotFoundError(f"session not found: {query}")
        unique = {self._safe_resolve(item[1].path): item for item in matches}
        if len(unique) > 1:
            raise AmbiguousSessionError(
                query, [str(item[1].path) for item in unique.values()]
            )
        return next(iter(unique.values()))

    def _backend_identity(
        self, provider: str | None, preferred_home: Path
    ) -> tuple[str, str | None] | None:
        """Resolve a provider alias without assuming the alias is the backend."""
        if provider is None:
            return None
        homes = [
            preferred_home,
            self.default_source_home(),
            self.config.source_home,
            *(profile.path for profile in self.list_profiles()),
        ]
        identities: set[tuple[str, str | None]] = set()
        seen: set[Path] = set()
        for home in homes:
            resolved = self._safe_resolve(home)
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

    def clone_session_for_profile(
        self, query: str, target_home: Path, *, allow_lossy: bool = True
    ) -> SessionCloneResult:
        src_home, session = self.find_session(query)
        provider = sessions_mod.configured_model_provider(target_home)
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

    def candidate_source_homes(self, target_home: Path) -> list[HomeRef]:
        """Source homes usable for migration into ``target_home`` (excludes it)."""
        target = self._safe_resolve(target_home)
        source = self._safe_resolve(self.config.source_home)
        refs: list[HomeRef] = []
        if source != target:
            refs.append(self.describe_home(self.config.source_home))
        for profile in self.list_profiles():
            resolved = self._safe_resolve(profile.path)
            if resolved not in (target, source):
                refs.append(HomeRef(resolved, HomeKind.PROFILE, profile.name))
        return refs

    # ---------------------------------------------------------------- sharing

    def share_sessions(self, profile: str, source_ref: str = REF_SOURCE) -> list[LinkAction]:
        """Symlink a profile's sessions/history/db to a source home.

        Non-destructive: existing real files are backed up (``.backup.<n>``)
        before a symlink replaces them, and identical symlinks are left alone.
        """
        profile_path = self.profile_home(profile, must_exist=True)
        source_home = self.resolve_home_ref(source_ref).path
        actions = self.link_shared(profile_path, source_home)
        profile_state.record_sync_type(profile_path, "sessions_shared")
        return actions

    def link_shared(self, profile_path: Path, source_home: Path) -> list[LinkAction]:
        """Link session artifacts without changing the profile sync state."""
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

    # Kept as a compatibility shim for callers that used the old internal
    # helper. New code should use :meth:`link_shared` or :meth:`share_sessions`.
    def _link_shared(self, profile_path: Path, source_home: Path) -> list[LinkAction]:
        return self.link_shared(profile_path, source_home)

    # ----------------------------------------------------------------- doctor

    def doctor(self) -> DoctorReport:
        bin_dir = str(self.config.bin_dir)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        effective_cmd = self.config.effective_codex_cmd
        codex_path = shutil.which(effective_cmd)
        return DoctorReport(
            codex_cmd=self.config.codex_cmd,
            codex_wrapper=self.config.codex_wrapper,
            effective_codex_cmd=effective_cmd,
            codex_args=self.config.codex_args,
            source_home=self.config.source_home,
            profile_root=self.config.profile_root,
            bin_dir=self.config.bin_dir,
            manager_bin_name=self.config.manager_bin_name,
            bin_on_path=bin_dir in path_entries,
            codex_path=codex_path,
        )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def _resolve_existing(self, path: Path) -> Path:
        if not path.exists():
            raise HomeNotFoundError(f"directory not found: {path}")
        return self._safe_resolve(path)

    @staticmethod
    def _force_symlink(source: Path, link: Path) -> None:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)

    def _backup(self, path: Path) -> Path:
        """Move ``path`` aside to a unique ``.backup.N`` name and return it."""
        n = 1
        while True:
            backup = path.with_name(f"{path.name}.backup.{n}")
            if not backup.exists():
                break
            n += 1
        shutil.move(str(path), str(backup))
        return backup
