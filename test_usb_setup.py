import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtWidgets as W
import usb_setup as setup


class TerminalTests(unittest.TestCase):
    def test_ubuntu_ptyxis_only_uses_standalone_and_literal_arguments(self):
        folder = Path('/tmp/setup with spaces; $name')
        files = [folder / name for name in ('runner.sh', 'helper.sh', 'result')]
        with patch.object(setup.shutil, 'which', side_effect=lambda name: '/usr/bin/ptyxis' if name == 'ptyxis' else None):
            program, arguments = setup.terminal_command(*files)
        self.assertEqual(program, '/usr/bin/ptyxis')
        self.assertIn('--standalone', arguments)
        self.assertEqual(arguments[arguments.index('--') + 1:], ['/bin/bash', *map(str, files)])

    def test_existing_terminals_still_launch_without_a_shell_string(self):
        files = [Path('/tmp/a b') / name for name in ('runner', 'helper', 'result')]
        for terminal in ('konsole', 'gnome-terminal', 'xfce4-terminal', 'xterm', 'kitty', 'alacritty'):
            with self.subTest(terminal=terminal), patch.object(setup.shutil, 'which',
                    side_effect=lambda name: '/usr/bin/' + name if name == terminal else None):
                program, arguments = setup.terminal_command(*files)
                self.assertEqual(program, '/usr/bin/' + terminal)
                self.assertEqual(arguments[-4:], ['/bin/bash', *map(str, files)])

    def test_no_terminal_reports_available_choices(self):
        with patch.object(setup.shutil, 'which', return_value=None), self.assertRaisesRegex(RuntimeError, 'Ptyxis'):
            setup.terminal_command(Path('/tmp/runner'), Path('/tmp/helper'), Path('/tmp/result'))

    def test_system_terminal_gets_original_libraries_and_fonts(self):
        for original in ('', '/custom/fonts.conf'):
            with self.subTest(original=original), patch.dict(os.environ, {
                    'LD_LIBRARY_PATH': '/bundle/libs', 'LD_LIBRARY_PATH_ORIG': '/system/libs',
                    'FONTCONFIG_FILE': '/bundle/fonts.conf', 'TRACEFORGE_FONTCONFIG_FILE_ORIG': original,
                    'QT_PLUGIN_PATH': '/bundle/qt', 'PYTHONHOME': '/bundle/python',
                    'DISPLAY': ':123', 'WAYLAND_DISPLAY': 'wayland-1'}):
                environment = setup.system_environment()
                self.assertEqual(environment.value('LD_LIBRARY_PATH'), '/system/libs')
                self.assertEqual(environment.value('FONTCONFIG_FILE'), original)
                self.assertEqual(environment.contains('FONTCONFIG_FILE'), bool(original))
                for name in ('TRACEFORGE_FONTCONFIG_FILE_ORIG', 'QT_PLUGIN_PATH', 'PYTHONHOME'):
                    self.assertFalse(environment.contains(name))
                self.assertEqual(environment.value('DISPLAY'), ':123')
                self.assertEqual(environment.value('WAYLAND_DISPLAY'), 'wayland-1')

    def test_unbundled_font_preferences_are_preserved(self):
        with patch.dict(os.environ, {'FONTCONFIG_FILE': '/custom/fonts.conf'}, clear=True):
            environment = setup.system_environment()
            self.assertEqual(environment.value('FONTCONFIG_FILE'), '/custom/fonts.conf')
            self.assertFalse(environment.contains('LD_LIBRARY_PATH'))


class SetupDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = W.QApplication.instance() or W.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.rule = self.root / 'rule'
        for mock in (patch.object(setup, 'RULE_PATH', self.rule),
                     patch.object(setup, 'data_directory', return_value=self.root / 'data')):
            mock.start()
            self.addCleanup(mock.stop)
        self.dialog = setup.USBSetupDialog()
        self.addCleanup(self.dialog.close)
        self.dialog.process = Mock()
        self.dialog.process.state.return_value = QtCore.QProcess.ProcessState.NotRunning
        self.dialog.process.readAllStandardOutput.return_value = b''
        self.dialog.result_path = self.root / 'result'

    def test_success_requires_result_and_actual_rule_then_creates_marker(self):
        self.rule.write_text(setup.RULE_TEXT + '\n')
        self.dialog.result_path.write_text('0\n')
        self.dialog.install_finished(0, QtCore.QProcess.ExitStatus.NormalExit)
        self.assertEqual(self.dialog.result(), W.QDialog.DialogCode.Accepted)
        self.assertEqual(json.loads((self.root / 'data/usb-setup.json').read_text())['usb_id'], '0783:5725')

    def test_closed_window_or_failed_sudo_never_marks_setup_complete(self):
        self.rule.write_text(setup.RULE_TEXT + '\n')
        for result in ('', '1'):
            with self.subTest(result=result):
                self.dialog.result_path.write_text(result)
                self.dialog.install_finished(0, QtCore.QProcess.ExitStatus.NormalExit)
                self.assertNotEqual(self.dialog.result(), W.QDialog.DialogCode.Accepted)
                self.assertFalse((self.root / 'data/usb-setup.json').exists())
                self.assertTrue(self.dialog.accept_button.isEnabled())

    def test_reported_success_without_rule_cannot_open_app(self):
        self.dialog.result_path.write_text('0\n')
        self.dialog.install_finished(0, QtCore.QProcess.ExitStatus.NormalExit)
        self.assertNotEqual(self.dialog.result(), W.QDialog.DialogCode.Accepted)
        self.assertFalse((self.root / 'data/usb-setup.json').exists())


class SetupRunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='traceforge setup ')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        sudo = self.root / 'sudo'
        sudo.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$TEST_SUDO_ARGS"\nexit "$TEST_SUDO_STATUS"\n')
        sudo.chmod(0o700)
        self.result = self.root / 'result'
        self.helper = self.root / 'helper with spaces.sh'

    def run_to_prompt(self, status):
        master, slave = pty.openpty()
        environment = dict(os.environ, PATH=str(self.root), TEST_SUDO_STATUS=str(status),
                           TEST_SUDO_ARGS=str(self.root / 'sudo-args'))
        bootstrap = 'import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv("/bin/bash",sys.argv[1:])'
        process = subprocess.Popen([sys.executable, '-c', bootstrap, 'bash',
            str(setup.ROOT / 'usb-setup-terminal.sh'), str(self.helper), str(self.result)],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=environment)
        os.close(slave)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.addCleanup(lambda: self.close_fd(master))
        output = b''
        deadline = time.monotonic() + 5
        while b'Press any key' not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 8192)
        self.assertIn(b'Press any key', output)
        self.assertIsNone(process.poll())
        self.assertFalse(self.result.exists())
        self.assertEqual((self.root / 'sudo-args').read_bytes().split(b'\0')[:-1],
                         [b'--', b'/bin/bash', b'-x', os.fsencode(self.helper)])
        return process, master

    @staticmethod
    def close_fd(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_success_waits_for_key_before_publishing_result(self):
        process, master = self.run_to_prompt(0)
        os.write(master, b' ')
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(self.result.read_text().strip(), '0')

    def test_failed_sudo_returns_failure_after_keypress(self):
        process, master = self.run_to_prompt(7)
        os.write(master, b' ')
        self.assertEqual(process.wait(timeout=5), 7)
        self.assertEqual(self.result.read_text().strip(), '7')

    def test_closing_terminal_before_key_does_not_publish_success(self):
        process, master = self.run_to_prompt(0)
        os.close(master)
        self.assertNotEqual(process.wait(timeout=5), 0)
        self.assertFalse(self.result.exists())


if __name__ == '__main__':
    unittest.main()
