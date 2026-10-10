import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtWidgets as W
import shortcuts as s


class ShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / 'Downloads' / 'Scope.AppImage'
        self.image.parent.mkdir()
        self.image.touch()
        self.targets = {'desktop': self.root / 'Desktop' / s.ENTRY_NAME,
                        'menu': self.root / 'applications' / s.ENTRY_NAME}
        for mocked in (patch.dict(os.environ, {'HANTEK_DATA_DIR': str(self.root / 'data')}),
                       patch.object(s, 'destinations', return_value=self.targets),
                       patch.object(s, 'appimage_path', side_effect=lambda: self.image),
                       patch.object(s, 'refresh_desktop_database')):
            mocked.start()
            self.addCleanup(mocked.stop)

    def click_dialog(self, method, desktop=True, menu=True):
        def click():
            dialog = self.app.activeModalWidget()
            dialog.checks['desktop'].setChecked(desktop)
            dialog.checks['menu'].setChecked(menu)
            getattr(dialog, method).click()
        QtCore.QTimer.singleShot(0, click)
        s.ensure_shortcuts(force=s.read_preferences() is not None)

    def test_skip_is_remembered_without_launchers(self):
        self.click_dialog('skip_button')
        self.assertFalse(s.read_preferences()['desktop'])
        with patch.object(s, 'ShortcutDialog', side_effect=AssertionError('asked again')):
            s.ensure_shortcuts()
        self.assertTrue(all(not path.exists() for path in self.targets.values()))

    def test_move_rename_updates_both_once(self):
        self.click_dialog('accept_button')
        before = {key: path.stat().st_mtime_ns for key, path in self.targets.items()}
        with patch.object(s, 'ShortcutDialog', side_effect=AssertionError('asked again')):
            s.ensure_shortcuts()
            self.assertEqual(before, {key: path.stat().st_mtime_ns for key, path in self.targets.items()})
            moved = self.root / 'Programs' / 'My renamed Scope.AppImage'
            moved.parent.mkdir()
            self.image.rename(moved)
            self.image = moved
            s.ensure_shortcuts()
        for target in self.targets.values():
            self.assertIn(s.exec_argument(moved), target.read_text())
            self.assertNotIn('/Downloads/', target.read_text())
            self.assertTrue(os.access(target, os.X_OK))

    def test_only_approved_target_created(self):
        self.click_dialog('accept_button', desktop=False)
        s.ensure_shortcuts()
        self.assertFalse(self.targets['desktop'].exists())
        self.assertTrue(self.targets['menu'].exists())

    def test_can_remove_managed_shortcuts_and_cancel_changes(self):
        self.click_dialog('accept_button')
        self.click_dialog('skip_button', desktop=False, menu=False)
        self.assertTrue(all(path.exists() for path in self.targets.values()))
        self.click_dialog('accept_button', desktop=False, menu=False)
        self.assertTrue(all(not path.exists() for path in self.targets.values()))
        s.ensure_shortcuts()
        self.assertTrue(all(not path.exists() for path in self.targets.values()))

    def test_existing_unrelated_file_preserved(self):
        target = self.targets['desktop']
        target.parent.mkdir()
        target.write_text('[Desktop Entry]\nName=Something else\n')
        with self.assertRaises(OSError):
            s.sync_shortcuts(self.image, {'desktop': True, 'menu': False}, explicit_consent=True)
        self.assertIn('Something else', target.read_text())

    def test_old_app_entry_requires_consent_and_is_backed_up(self):
        target = self.targets['menu']
        target.parent.mkdir()
        old = '[Desktop Entry]\nName=Hantek Linux Scope\nExec=/old/launch.sh\n'
        target.write_text(old)
        with self.assertRaises(OSError):
            s.sync_shortcuts(self.image, {'desktop': False, 'menu': True})
        s.sync_shortcuts(self.image, {'desktop': False, 'menu': True}, explicit_consent=True)
        backups = list((self.root / 'data' / 'shortcut-backups').glob('*.desktop'))
        self.assertEqual([p.read_text() for p in backups], [old])
        self.assertTrue(s.owned_entry(target))

    def test_symlink_is_never_overwritten_or_followed(self):
        unrelated = self.root / 'other'
        unrelated.write_text('unchanged')
        target = self.targets['desktop']
        target.parent.mkdir()
        target.symlink_to(unrelated)
        with self.assertRaises(OSError):
            s.sync_shortcuts(self.image, {'desktop': True, 'menu': False}, explicit_consent=True)
        self.assertEqual(unrelated.read_text(), 'unchanged')
        self.assertTrue(target.is_symlink())


if __name__ == '__main__':
    unittest.main()
