import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

from shared_data import SharedDataConflictError, SharedDataCoordinator

RUNTIME_DIR = Path(__file__).with_name('.test-runtime-shared')
RUNTIME_DIR.mkdir(exist_ok=True)


class SharedDataCoordinatorTests(unittest.TestCase):
    def make_root(self):
        root = RUNTIME_DIR / f'case-{uuid.uuid4().hex}'
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def make_shared_dir(self, root):
        data_dir = Path(root) / 'shared-data'
        data_dir.mkdir()
        (data_dir / '.tasklist-shared.json').write_text(
            json.dumps({
                'format': 'tasklist-google-drive-shared-data',
                'schema_version': 1,
            }),
            encoding='utf-8'
        )
        return data_dir

    def test_shared_mode_requires_sentinel(self):
        data_dir = self.make_root() / 'missing'
        data_dir.mkdir()
        coordinator = SharedDataCoordinator(data_dir, enabled=True)
        with self.assertRaises(RuntimeError):
            coordinator.validate_data_directory()

    def test_atomic_write_keeps_backup(self):
        data_dir = self.make_shared_dir(self.make_root())
        tasks_path = data_dir / 'tasks.csv'
        tasks_path.write_text('old\n', encoding='utf-8')
        coordinator = SharedDataCoordinator(data_dir, enabled=True)
        try:
            coordinator.atomic_write_data_file(
                str(tasks_path),
                lambda file_obj: file_obj.write('new\n')
            )
            self.assertEqual(tasks_path.read_text(encoding='utf-8'), 'new\n')
            backups = list((data_dir / 'backups').rglob('*-tasks.csv'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding='utf-8'), 'old\n')
        finally:
            coordinator.stop_session()

    def test_active_other_device_blocks_writes(self):
        data_dir = self.make_shared_dir(self.make_root())
        tasks_path = data_dir / 'tasks.csv'
        tasks_path.write_text('old\n', encoding='utf-8')
        coordinator = SharedDataCoordinator(data_dir, enabled=True)
        lease_dir = Path(coordinator.coordination_dir)
        lease_dir.mkdir(parents=True)
        (lease_dir / 'active-other-session.json').write_text(
            json.dumps({
                'device_id': 'other-mac',
                'pid': 99999,
                'heartbeat_epoch': time.time()
            }),
            encoding='utf-8'
        )

        with self.assertRaises(SharedDataConflictError):
            coordinator.atomic_write_data_file(
                str(tasks_path),
                lambda file_obj: file_obj.write('blocked\n')
            )
        self.assertEqual(tasks_path.read_text(encoding='utf-8'), 'old\n')

    def test_shared_mode_rejects_wrong_sentinel_schema(self):
        data_dir = self.make_root() / 'shared-data'
        data_dir.mkdir()
        (data_dir / '.tasklist-shared.json').write_text(
            json.dumps({
                'format': 'tasklist-google-drive-shared-data',
                'schema_version': 999,
            }),
            encoding='utf-8',
        )
        coordinator = SharedDataCoordinator(data_dir, enabled=True)

        with self.assertRaises(RuntimeError):
            coordinator.validate_data_directory()

    def test_fsync_unsupported_errors_are_allowed(self):
        unsupported_errnos = {
            errno.EINVAL,
            getattr(errno, 'ENOTSUP', errno.EINVAL),
            getattr(errno, 'EOPNOTSUPP', errno.EINVAL),
        }
        for error_number in unsupported_errnos:
            with self.subTest(error_number=error_number):
                file_obj = mock.Mock()
                file_obj.fileno.return_value = 123
                with mock.patch(
                    'shared_data.os.fsync',
                    side_effect=OSError(error_number, 'fsync unsupported'),
                ):
                    SharedDataCoordinator._flush_and_sync(file_obj)
                file_obj.flush.assert_called_once_with()

    def test_fsync_io_error_is_not_ignored(self):
        file_obj = mock.Mock()
        file_obj.fileno.return_value = 123

        with mock.patch(
            'shared_data.os.fsync',
            side_effect=OSError(errno.EIO, 'I/O error'),
        ):
            with self.assertRaises(OSError) as raised:
                SharedDataCoordinator._flush_and_sync(file_obj)

        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_malformed_same_device_pid_is_treated_as_active(self):
        data_dir = self.make_shared_dir(self.make_root())
        coordinator = SharedDataCoordinator(data_dir, enabled=True)

        self.assertTrue(coordinator._same_device_process_is_running({
            'device_id': coordinator.device_id,
            'pid': 'not-a-pid',
        }))

    def test_stop_session_joins_heartbeat_and_removes_lease(self):
        data_dir = self.make_shared_dir(self.make_root())
        coordinator = SharedDataCoordinator(data_dir, enabled=True)
        self.assertEqual(coordinator.ensure_session(), [])
        heartbeat_thread = coordinator._heartbeat_thread
        self.assertTrue(Path(coordinator.lease_path).is_file())

        coordinator.stop_session()

        self.assertFalse(heartbeat_thread.is_alive())
        self.assertFalse(Path(coordinator.lease_path).exists())

    def test_backup_retention_is_limited_per_data_file(self):
        data_dir = self.make_shared_dir(self.make_root())
        tasks_path = data_dir / 'tasks.csv'
        tasks_path.write_text('version-0\n', encoding='utf-8')
        coordinator = SharedDataCoordinator(data_dir, enabled=True)
        coordinator.backup_max_generations = 2
        try:
            for version in range(1, 5):
                coordinator.atomic_write_data_file(
                    str(tasks_path),
                    lambda file_obj, value=version: file_obj.write(
                        f'version-{value}\n'
                    ),
                )

            backups = list((data_dir / 'backups').rglob('*-tasks.csv'))
            self.assertEqual(len(backups), 2)
            self.assertEqual(
                tasks_path.read_text(encoding='utf-8'),
                'version-4\n',
            )
        finally:
            coordinator.stop_session()

    @unittest.skipUnless(os.name == 'nt', 'Windows-specific PID probe')
    def test_windows_pid_probe_does_not_terminate_process(self):
        data_dir = self.make_shared_dir(self.make_root())
        coordinator = SharedDataCoordinator(data_dir, enabled=True)
        process = subprocess.Popen([
            sys.executable,
            '-c',
            'import time; time.sleep(30)',
        ])
        try:
            self.assertTrue(coordinator._same_device_process_is_running({
                'device_id': coordinator.device_id,
                'pid': process.pid,
            }))
            self.assertIsNone(
                process.poll(),
                'The Windows PID existence probe terminated the process.',
            )
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
