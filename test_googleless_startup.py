import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


APP_PATH = Path(__file__).with_name('app.py')
os.environ['GOOGLE_SYNC_ENABLED'] = '0'


def load_app(module_name):
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tasklist = load_app('tasklist_googleless_tests')


class GooglelessStartupTests(unittest.TestCase):
    def test_default_disables_google_without_credentials(self):
        with patch.object(tasklist.os.path, 'exists', return_value=False):
            enabled = tasklist.resolve_google_sync_enabled(
                'auto',
                'missing-credentials.json'
            )
        self.assertFalse(enabled)

    def test_auto_enables_google_when_credentials_exist(self):
        with patch.object(tasklist.os.path, 'exists', return_value=True):
            enabled = tasklist.resolve_google_sync_enabled(
                'auto',
                'credentials.json'
            )
        self.assertTrue(enabled)

    def test_explicit_off_overrides_existing_credentials(self):
        with patch.object(tasklist.os.path, 'exists', return_value=True):
            enabled = tasklist.resolve_google_sync_enabled(
                '0',
                'credentials.json'
            )
        self.assertFalse(enabled)

    def test_disabled_sync_never_loads_google_service_or_worker(self):
        tasklist.GOOGLE_SYNC_ENABLED = False
        tasklist.SYNC_WORKER_STARTED = False

        self.assertIsNone(tasklist.get_google_service())
        tasklist.start_sync_worker()
        self.assertFalse(tasklist.SYNC_WORKER_STARTED)


if __name__ == '__main__':
    unittest.main()
