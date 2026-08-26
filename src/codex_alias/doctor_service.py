"""Environment diagnostics for codexalias."""

from __future__ import annotations

import os
import shutil

from .config import Config
from .models import DoctorReport


class DoctorService:
    """Build a read-only snapshot of the configured Codex environment."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def report(self) -> DoctorReport:
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
