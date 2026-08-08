# -*- coding: utf-8 -*-
"""Safe file writes and best-effort single-device coordination for shared data."""

import atexit
import datetime as dt
import errno
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid


class SharedDataConflictError(RuntimeError):
    """Raised when another recently active device owns the shared data."""


class SharedDataCoordinator:
    sentinel_format = 'tasklist-google-drive-shared-data'
    sentinel_schema_version = 1

    def __init__(self, data_dir, enabled=False, sentinel_name='.tasklist-shared.json'):
        self.data_dir = os.path.abspath(data_dir)
        self.enabled = bool(enabled)
        self.sentinel_path = os.path.join(self.data_dir, sentinel_name)
        self.device_id = self._safe_device_id(
            os.environ.get('TASKLIST_DEVICE_ID', socket.gethostname())
        )
        self.session_id = uuid.uuid4().hex
        self.lease_ttl_seconds = max(
            int(os.environ.get('TASKLIST_LEASE_TTL_SECONDS', '180')),
            60
        )
        self.heartbeat_seconds = max(
            int(os.environ.get('TASKLIST_HEARTBEAT_SECONDS', '30')),
            10
        )
        self._write_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._session_started = False
        self._heartbeat_started = False
        self._heartbeat_thread = None
        self._conflicts = []
        self._stop_event = threading.Event()
        self._started_at = None
        self.backup_max_generations = 500

    @staticmethod
    def _safe_device_id(value):
        cleaned = re.sub(r'[^0-9A-Za-z_.-]+', '-', (value or '').strip())
        return cleaned.strip('-') or 'device'

    @property
    def coordination_dir(self):
        return os.path.join(self.data_dir, '_tasklist_sync', 'leases')

    @property
    def lease_path(self):
        return os.path.join(
            self.coordination_dir,
            f'active-{self.device_id}-{self.session_id}.json'
        )

    def validate_data_directory(self):
        if not self.enabled:
            os.makedirs(self.data_dir, exist_ok=True)
            return

        if not os.path.isdir(self.data_dir):
            raise RuntimeError(
                f'Google Drive共有データが見つかりません: {self.data_dir}'
            )
        if not os.path.isfile(self.sentinel_path):
            raise RuntimeError(
                f'共有データの確認ファイルがありません: {self.sentinel_path}'
            )
        try:
            with open(self.sentinel_path, 'r', encoding='utf-8') as file_obj:
                sentinel = json.load(file_obj)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f'共有データの確認ファイルが壊れています: {self.sentinel_path}'
            ) from exc
        if (
            not isinstance(sentinel, dict)
            or sentinel.get('format') != self.sentinel_format
            or sentinel.get('schema_version') != self.sentinel_schema_version
        ):
            raise RuntimeError(
                f'共有データの形式またはバージョンが一致しません: '
                f'{self.sentinel_path}'
            )

    @staticmethod
    def _flush_and_sync(file_obj):
        file_obj.flush()
        try:
            os.fsync(file_obj.fileno())
        except OSError as exc:
            # Some virtual file systems do not expose fsync. The same-directory
            # atomic replace still prevents readers from seeing a partial CSV.
            unsupported_errnos = {errno.EINVAL}
            for name in ('ENOTSUP', 'EOPNOTSUPP'):
                value = getattr(errno, name, None)
                if value is not None:
                    unsupported_errnos.add(value)
            if exc.errno not in unsupported_errnos:
                raise

    def atomic_write_text(self, path, writer):
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f'.{os.path.basename(path)}.',
            suffix='.tmp',
            dir=directory,
            text=True
        )
        try:
            with os.fdopen(fd, 'w', newline='', encoding='utf-8') as file_obj:
                writer(file_obj)
                self._flush_and_sync(file_obj)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _backup_data_file(self, path):
        if not self.enabled or not os.path.isfile(path):
            return

        now = dt.datetime.now()
        backup_dir = os.path.join(
            self.data_dir,
            'backups',
            now.strftime('%Y-%m-%d')
        )
        os.makedirs(backup_dir, exist_ok=True)
        backup_name = (
            f'{now.strftime("%H%M%S-%f")}-{self.device_id}-'
            f'{os.path.basename(path)}'
        )
        shutil.copy2(path, os.path.join(backup_dir, backup_name))
        self._prune_backups(path)

    def _prune_backups(self, source_path):
        backup_root = os.path.join(self.data_dir, 'backups')
        if not os.path.isdir(backup_root):
            return

        suffix = f'-{os.path.basename(source_path)}'
        candidates = []
        for directory, _, filenames in os.walk(backup_root):
            for filename in filenames:
                if filename.endswith(suffix):
                    candidates.append(os.path.join(directory, filename))

        candidates.sort(reverse=True)
        for stale_path in candidates[self.backup_max_generations:]:
            try:
                os.remove(stale_path)
            except OSError:
                # A later save retries retention cleanup. An old backup that
                # cannot be removed must not prevent the current task save.
                continue

    def atomic_write_data_file(self, path, writer, create_backup=True):
        with self._write_lock:
            if self.enabled:
                self.assert_write_allowed()
            if create_backup:
                self._backup_data_file(path)
            self.atomic_write_text(path, writer)

    @staticmethod
    def _windows_process_is_running(pid):
        """Check a Windows PID without sending it a signal.

        On Windows, ``os.kill(pid, 0)`` calls TerminateProcess rather than
        performing the signal-0 existence probe available on POSIX.
        """
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87

        if pid > 0xFFFFFFFF:
            return False

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        open_process.restype = wintypes.HANDLE

        get_exit_code_process = kernel32.GetExitCodeProcess
        get_exit_code_process.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_exit_code_process.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            # ERROR_INVALID_PARAMETER means that the PID does not exist.
            # Access-denied and other failures are treated as "running" so an
            # uncertain probe never permits a potentially conflicting write.
            return ctypes.get_last_error() != error_invalid_parameter

        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code_process(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            close_handle(handle)

    def _same_device_process_is_running(self, lease):
        if lease.get('device_id') != self.device_id:
            return None
        try:
            pid = int(lease.get('pid') or 0)
        except (TypeError, ValueError, OverflowError):
            # A fresh but malformed same-device lease is treated as active.
            # Ignoring it could permit two writers to use the shared CSVs.
            return True
        if pid <= 0:
            return True
        if pid == os.getpid():
            return True
        if os.name == 'nt':
            return self._windows_process_is_running(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def active_leases(self):
        if not self.enabled or not os.path.isdir(self.coordination_dir):
            return []

        now = time.time()
        active = []
        for name in os.listdir(self.coordination_dir):
            if not name.startswith('active-') or not name.endswith('.json'):
                continue
            path = os.path.join(self.coordination_dir, name)
            if os.path.abspath(path) == os.path.abspath(self.lease_path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as file_obj:
                    lease = json.load(file_obj)
                heartbeat = float(lease.get('heartbeat_epoch') or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

            if now - heartbeat > self.lease_ttl_seconds:
                continue
            local_process_state = self._same_device_process_is_running(lease)
            if local_process_state is False:
                continue
            active.append(lease)
        return active

    def _write_lease(self):
        now = dt.datetime.now(dt.timezone.utc)
        if self._started_at is None:
            self._started_at = now.isoformat()
        payload = {
            'device_id': self.device_id,
            'hostname': socket.gethostname(),
            'pid': os.getpid(),
            'session_id': self.session_id,
            'started_at': self._started_at,
            'heartbeat_at': now.isoformat(),
            'heartbeat_epoch': time.time()
        }
        self.atomic_write_text(
            self.lease_path,
            lambda file_obj: json.dump(
                payload,
                file_obj,
                ensure_ascii=False,
                indent=2
            )
        )

    def refresh_session(self):
        if (
            not self.enabled
            or not self._session_started
            or self._stop_event.is_set()
        ):
            return []
        with self._session_lock:
            if self._stop_event.is_set():
                return []
            self._write_lease()
            self._conflicts = self.active_leases()
            return list(self._conflicts)

    def _heartbeat_loop(self):
        while not self._stop_event.wait(self.heartbeat_seconds):
            try:
                self.refresh_session()
            except Exception:
                # A request will surface storage failures to the user. Keep the
                # background thread alive so a temporary Drive outage can recover.
                continue

    def stop_session(self):
        if not self.enabled:
            return
        self._stop_event.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._session_lock:
            try:
                if os.path.exists(self.lease_path):
                    os.remove(self.lease_path)
            except OSError:
                pass
            self._session_started = False
            self._heartbeat_started = False
            self._heartbeat_thread = None

    def ensure_session(self):
        if not self.enabled:
            return []

        self.validate_data_directory()
        with self._session_lock:
            if not self._session_started:
                self._conflicts = self.active_leases()
                if self._conflicts:
                    return list(self._conflicts)
                os.makedirs(self.coordination_dir, exist_ok=True)
                self._write_lease()
                self._session_started = True
                atexit.register(self.stop_session)

            if not self._heartbeat_started:
                thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                self._heartbeat_thread = thread
                thread.start()
                self._heartbeat_started = True

            self._conflicts = self.active_leases()
            return list(self._conflicts)

    def conflict_message(self, conflicts=None):
        conflicts = self._conflicts if conflicts is None else conflicts
        devices = ', '.join(
            sorted({item.get('device_id', '他のPC') for item in conflicts})
        )
        return (
            f'他のPC（{devices}）が共有データを使用中です。'
            'そのPCでアプリを終了し、Google Driveの同期完了後に再試行してください。'
        )

    def assert_write_allowed(self):
        if not self.enabled:
            return
        conflicts = self.ensure_session()
        if conflicts:
            raise SharedDataConflictError(self.conflict_message(conflicts))
