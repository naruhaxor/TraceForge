import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from builder.bootstrap import ensure_python, extract_python, private_python, usable_python


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='traceforge test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {'filename': 'python.tar.gz', 'url': 'https://example.invalid/python.tar.gz', 'sha256': 'a' * 64}
        (self.root / 'builder').mkdir()
        (self.root / 'builder/sources.json').write_text(json.dumps({'python': self.spec}))
        self.args = SimpleNamespace(work_dir=self.root / 'build with spaces', portable=False)

    def archive(self, entries):
        archive = self.root / 'python.tar'
        with tarfile.open(archive, 'w') as tar:
            for name, content, link in entries:
                entry = tarfile.TarInfo(name)
                if link is None:
                    entry.size = len(content)
                    entry.mode = 0o755
                    tar.addfile(entry, io.BytesIO(content))
                else:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = link
                    tar.addfile(entry)
        return archive

    def test_private_python_extracts_executable_and_relative_link(self):
        archive = self.archive([('python/bin/python3.12', b'python', None), ('python/bin/python3', b'', 'python3.12')])
        target = self.root / 'install'
        extract_python(archive, target)
        self.assertEqual((target / 'python/bin/python3').read_bytes(), b'python')
        self.assertEqual((target / 'python/bin/python3.12').stat().st_mode & 0o777, 0o755)

    def test_python_archive_rejects_unsafe_paths_links_and_link_children(self):
        cases = [
            [('../escape', b'', None)],
            [('python/../../escape', b'', None)],
            [('python/bin/python3', b'', '/bin/sh')],
            [('python/bin/python3', b'', '../../../outside')],
            [('python/a', b'', 'b'), ('python/a/file', b'bad', None)],
            [('python/a', b'', 'a')],
            [('python/file', b'one', None), ('python/file', b'two', None)],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                target = self.root / 'install'
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    extract_python(self.archive(entries), target)
                self.assertFalse(target.exists())
                self.assertFalse((self.root / 'escape').exists())

    def test_incompatible_python_downloads_once_and_reuses_completed_install(self):
        archive = self.archive([('python/bin/python3', b'python', None)])
        with patch('builder.bootstrap.fetch', return_value=archive) as fetch, patch('builder.bootstrap.usable_python', return_value=True):
            first = private_python(self.args.work_dir, self.spec)
            second = private_python(self.args.work_dir, self.spec)
        self.assertEqual(first, second)
        self.assertTrue(first.is_file())
        fetch.assert_called_once()

    def test_failed_install_is_not_reused(self):
        archive = self.archive([('python/bin/python3', b'broken', None)])
        with patch('builder.bootstrap.fetch', return_value=archive), patch('builder.bootstrap.usable_python', return_value=False), self.assertRaises(RuntimeError):
            private_python(self.args.work_dir, self.spec)
        self.assertFalse((self.args.work_dir / ('python-' + self.spec['sha256'][:16])).exists())
        self.assertFalse([path for path in self.args.work_dir.glob('.python-*') if path.is_dir()])

    def test_compatible_python_does_not_download(self):
        with patch('builder.bootstrap.usable_python', return_value=True), patch('builder.bootstrap.private_python') as download:
            ensure_python(self.args, self.root)
        download.assert_not_called()

    def test_older_python_reexec_preserves_build_arguments(self):
        python = self.root / 'private python/bin/python3'
        argv = ['build.py', '--work-dir', str(self.args.work_dir), '--vendor-zip', '/a zip.zip', '--no-gui']
        with patch('builder.bootstrap.usable_python', return_value=False), patch('builder.bootstrap.private_python', return_value=python), patch('sys.argv', argv), patch('os.execv') as execute:
            ensure_python(self.args, self.root)
        execute.assert_called_once_with(str(python), [str(python), str(self.root / 'build.py'), *argv[1:]])

    def test_portable_build_uses_pinned_python_even_on_newer_host(self):
        self.args.portable = True
        python = self.root / 'private/bin/python3'
        with patch('builder.bootstrap.usable_python', return_value=True), patch('builder.bootstrap.private_python', return_value=python) as download, patch('os.execv'):
            ensure_python(self.args, self.root)
        download.assert_called_once()

    def test_missing_python_is_unusable(self):
        self.assertFalse(usable_python(self.root / 'missing'))


if __name__ == '__main__':
    unittest.main()
