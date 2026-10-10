from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from builder.system_deps import ensure_bubblewrap, ensure_system, install_commands, missing_requirements
from builder.ui import Cancelled


class SystemDependencyTests(unittest.TestCase):
    def test_missing_graphics_library_is_detected(self):
        def load(name):
            if name == 'libxcb-cursor.so.0':
                raise OSError('not installed')
        with tempfile.TemporaryDirectory() as folder, patch('shutil.which', return_value='/usr/bin/tool'), patch('subprocess.run', return_value=Mock(returncode=0)), patch('ctypes.CDLL', side_effect=load):
            self.assertEqual(missing_requirements(Path(folder)), ['libxcb-cursor.so.0'])

    def test_apt_installs_build_and_graphics_dependencies_without_system_pip(self):
        with patch('shutil.which', side_effect=lambda name: '/usr/bin/apt-get' if name == 'apt-get' else None):
            commands = install_commands()
            self.assertIn('libxcb-cursor0', commands[-1])
            self.assertIn('zlib1g-dev', commands[-1])
            self.assertNotIn('python3-pip', commands[-1])
            self.assertEqual(install_commands(True)[-1], ['sudo', 'apt-get', 'install', '-y', 'bubblewrap'])

    def test_cancel_does_not_execute_package_manager(self):
        prompt, run = Mock(), Mock()
        prompt.confirm_install.return_value = False
        with patch('builder.system_deps.missing_requirements', return_value=['cmake']), patch('builder.system_deps.install_commands', return_value=[['sudo', 'apt-get', 'install', 'cmake']]), self.assertRaises(Cancelled):
            ensure_system(Path('/unused'), prompt, run)
        run.assert_not_called()

    def test_install_rechecks_requirements(self):
        prompt, run = Mock(), Mock()
        with patch('builder.system_deps.missing_requirements', side_effect=[['cmake'], []]), patch('builder.system_deps.install_commands', return_value=[['sudo', 'install', 'cmake']]), patch('sys.stdin.isatty', return_value=True):
            ensure_system(Path('/unused'), prompt, run, approved=True)
        run.assert_called_once_with(['sudo', 'install', 'cmake'])

    def test_portable_offers_bubblewrap_install(self):
        with patch('shutil.which', side_effect=[None, '/usr/bin/bwrap']), patch('builder.system_deps.install') as install:
            ensure_bubblewrap(Mock(), Mock())
        self.assertTrue(install.call_args.kwargs['bubblewrap'])


if __name__ == '__main__':
    unittest.main()
