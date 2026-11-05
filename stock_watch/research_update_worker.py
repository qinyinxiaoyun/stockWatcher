"""Worker-only research queue composition and consistent pre-apply backups."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from .db import connect
from .research import ResearchStore
from .research_cli import _export, _safe_directory


class ResearchUpdateService:
    def __init__(self, settings):
        self.settings = settings

    def _backup(self, conn, *args, **kwargs):
        if conn.in_transaction:
            raise RuntimeError('backup must run outside a write transaction')
        directory = _safe_directory(Path(self.settings.database_path).parent / 'research' / 'backups')
        fd, temporary = tempfile.mkstemp(prefix='.backup-', suffix='.sqlite', dir=directory)
        os.close(fd)
        temporary = Path(temporary)
        target = directory / (temporary.stem.removeprefix('.backup-') + '.sqlite')
        try:
            destination = sqlite3.connect(temporary)
            try:
                conn.backup(destination)
            finally:
                destination.close()
            if target.exists() or target.is_symlink():
                raise ValueError('backup target already exists')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def run_one(self, now=None):
        if not getattr(self.settings, 'research_api_enabled', False):
            return False
        from .research_updates import ResearchUpdates
        conn = connect(self.settings.database_path)
        try:
            updates = ResearchUpdates(conn, allowed_companies=self.settings.research_allowed_companies)
            return updates.process_one(
                now=now.isoformat() if isinstance(now, datetime) else (now or datetime.now(timezone.utc).isoformat()),
                apply_enabled=self.settings.research_apply_enabled,
                before_apply=lambda *args, **kwargs: self._backup(conn),
                exporter=lambda company_id: _export(ResearchStore(conn), self.settings.research_report_dir),
            )
        finally:
            conn.close()
