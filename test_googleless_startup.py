import importlib.util
import os
from pathlib import Path
import unittest


APP_PATH = Path(__file__).with_name('app.py')
os.environ['GOOGLE_SYNC_ENABLED'] = '0'


def load_app(module_name):
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tasklist = load_app('tasklist_googleless_tests')


class GooglelessStartupTests(unittest.TestCase):
    def test_default_is_disabled(self):
        self.assertFalse(tasklist.google_sync_enabled_from_setting(None))
        self.assertFalse(tasklist.google_sync_enabled_from_setting('0'))
        self.assertFalse(tasklist.google_sync_enabled_from_setting('auto'))

    def test_only_explicit_truthy_setting_enables_google(self):
        self.assertTrue(tasklist.google_sync_enabled_from_setting('1'))
        self.assertTrue(tasklist.google_sync_enabled_from_setting('true'))
        self.assertTrue(tasklist.google_sync_enabled_from_setting('on'))

    def test_disabled_sync_never_loads_google_service_or_worker(self):
        tasklist.GOOGLE_SYNC_ENABLED = False
        tasklist.SYNC_WORKER_STARTED = False

        self.assertIsNone(tasklist.get_google_service())
        tasklist.start_sync_worker()
        self.assertFalse(tasklist.SYNC_WORKER_STARTED)


if __name__ == '__main__':
    unittest.main()
