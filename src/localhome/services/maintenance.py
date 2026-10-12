"""Maintenance actions for the climate admin panel: clear the history
(the samples, each day's totals and the heat meter's readings), clear
every schedule, reset to a fresh installation, and back up the
configuration.

Every destructive action first saves a backup on the board itself
(`config/backups/`), so a wrong tap can be undone by copying files back.

The reset only removes what LocalHome itself created at runtime - the
panel's device mapping, schedules, manual values, cached readings,
shutter positions and the history. Everything written by hand stays:
config.yaml, devices/credential files, the MQTT broker settings. After
the restart that follows, the installation behaves like a new one on
the same hardware and network.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import time
import zipfile

logger = logging.getLogger(__name__)

# Automatic backups kept on the board before the oldest is removed.
KEEP_CONFIG_BACKUPS = 10
KEEP_HISTORY_BACKUPS = 1
# A history copy is skipped (the action still runs) unless the disk keeps
# at least this much free after it: a backup must never fill the disk.
MIN_FREE_AFTER_BACKUP = 500 * 1024 * 1024

# Files that hold credentials: kept in the on-board backups (they never
# leave the board), left out of the downloadable one.
CREDENTIAL_FILES = ("devices.json", "tuya_cloud.json")
CREDENTIAL_PREFIX = "secrets_"


class Maintenance:
    def __init__(self, config_dir: str, history_store, schedule_store=None, runtime_files: list[str] = (),
                 history_files: list[str] = ()):
        self.config_dir = config_dir
        self.backup_dir = os.path.join(config_dir, "backups")
        self.history_store = history_store
        self.schedule_store = schedule_store
        # Absolute paths of the files the reset removes (see module docstring).
        self.runtime_files = list(runtime_files)
        # And those that go with the history - the heat meter's readings
        # beside the plant's hours: removed with it, and by the reset.
        self.history_files = list(history_files)

    # ------------------------------------------------------------ backups --

    def _config_files(self) -> list[str]:
        """config.yaml and every JSON file next to it, minus the shipped
        *.example.* templates."""
        names = []
        for name in sorted(os.listdir(self.config_dir)):
            path = os.path.join(self.config_dir, name)
            if not os.path.isfile(path) or ".example." in name:
                continue
            if name.endswith(".json") or name.endswith((".yaml", ".yml")):
                names.append(name)
        return names

    @staticmethod
    def _is_credential(name: str) -> bool:
        return name.startswith(CREDENTIAL_PREFIX) or name in CREDENTIAL_FILES

    def backup_zip(self, include_credentials: bool) -> bytes:
        """The configuration as a zip. Without credentials (the
        downloadable one): credential files are left out and the MQTT
        broker's password is blanked."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self._config_files():
                if not include_credentials and self._is_credential(name):
                    continue
                path = os.path.join(self.config_dir, name)
                if not include_credentials and name == "mqtt_broker.json":
                    try:
                        with open(path, encoding="utf-8") as fh:
                            broker = json.load(fh)
                    except (OSError, ValueError):
                        continue
                    if broker.get("password"):
                        broker["password"] = ""
                    zf.writestr(name, json.dumps(broker, indent=2))
                    continue
                zf.write(path, name)
        return buffer.getvalue()

    def save_backup(self, reason: str) -> str:
        """A full backup (credentials included) saved on the board.
        Returns its file name."""
        os.makedirs(self.backup_dir, exist_ok=True)
        name = f"backup-{time.strftime('%Y%m%d-%H%M%S')}-{reason}.zip"
        with open(os.path.join(self.backup_dir, name), "wb") as fh:
            fh.write(self.backup_zip(include_credentials=True))
        self._prune("backup-", ".zip", KEEP_CONFIG_BACKUPS)
        return name

    def save_history_backup(self) -> str | None:
        """A copy of the history database, or None when the disk is too
        full to hold one safely (the caller carries on without it)."""
        os.makedirs(self.backup_dir, exist_ok=True)
        # Only the newest copy is kept: drop the old one before making room.
        self._prune("history-", ".db", 0)
        try:
            size = os.path.getsize(self.history_store.db_path)
            free = shutil.disk_usage(self.backup_dir).free
        except OSError:
            size, free = 0, 0
        if free - size < MIN_FREE_AFTER_BACKUP:
            logger.warning("Not enough disk space for a history backup (%d MB free) - skipped", free // 1_000_000)
            return None
        name = f"history-{time.strftime('%Y%m%d-%H%M%S')}.db"
        self.history_store.backup_to(os.path.join(self.backup_dir, name))
        self._prune("history-", ".db", KEEP_HISTORY_BACKUPS)
        return name

    def _prune(self, prefix: str, suffix: str, keep: int) -> None:
        files = sorted(n for n in os.listdir(self.backup_dir) if n.startswith(prefix) and n.endswith(suffix))
        for name in files[:-keep] if keep else files:
            try:
                os.remove(os.path.join(self.backup_dir, name))
            except OSError:
                logger.warning("Could not remove old backup %s", name)

    def list_backups(self) -> list[dict]:
        if not os.path.isdir(self.backup_dir):
            return []
        backups = []
        for name in sorted(os.listdir(self.backup_dir), reverse=True):
            path = os.path.join(self.backup_dir, name)
            if os.path.isfile(path):
                backups.append({"name": name, "bytes": os.path.getsize(path), "mtime": int(os.path.getmtime(path))})
        return backups

    # ------------------------------------------------------------ actions --

    def clear_history(self) -> dict:
        """The samples, each day's totals and the files that go with them
        (the heat meter's readings) - all of it backed up first."""
        backup = self.save_history_backup()
        files = [p for p in self.history_files if os.path.exists(p)]
        files_backup = self.save_backup("history") if files else None
        removed = self.history_store.clear()
        for path in files:
            os.remove(path)
        logger.warning("Maintenance: history cleared (%d rows, %s) - backups %s, %s", removed,
                       [os.path.basename(p) for p in files], backup, files_backup)
        return {"rows_removed": removed, "backup": backup, "files_backup": files_backup}

    def clear_schedules(self) -> dict:
        if self.schedule_store is None:
            raise ValueError("no thermostat configured")
        backup = self.save_backup("schedules")
        self.schedule_store.clear_schedules()
        logger.warning("Maintenance: every schedule cleared - backup %s", backup)
        return {"backup": backup}

    def factory_reset(self) -> dict:
        """Backs everything up, then removes what LocalHome created. The
        caller restarts the process afterwards - the running one still
        holds the old mapping in memory."""
        backup = self.save_backup("factory-reset")
        history_backup = self.save_history_backup()
        self.history_store.clear()
        removed = []
        for path in self.runtime_files + self.history_files:
            if os.path.exists(path):
                os.remove(path)
                removed.append(os.path.basename(path))
        logger.warning("Maintenance: factory reset - removed %s, history cleared - backups %s, %s",
                       removed, backup, history_backup)
        return {"removed": removed, "backup": backup, "history_backup": history_backup}
