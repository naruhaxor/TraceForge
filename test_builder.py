import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from builder.downloads import extract_setup, extract_tar, fetch, obtain_vendor
from builder.ui import Cancelled, Prompt


class BuildFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.source = {'filename': 'HT1008_Software.zip', 'url': 'https://example.invalid/software.zip',
                       'sha256': hashlib.sha256(b'test archive').hexdigest()}

    def test_local_zip_skips_prompt_and_preserves_original(self):
        original = self.root / self.source['filename']
        original.write_bytes(b'test archive')
        prompt = Mock()
        result = obtain_vendor(self.source, self.root, self.work, prompt)
        self.assertEqual(result.read_bytes(), original.read_bytes())
        prompt.choose_vendor.assert_not_called()

    def test_locate_copies_to_working_folder(self):
        elsewhere = self.root / 'elsewhere.zip'
        elsewhere.write_bytes(b'test archive')
        prompt = Mock()
        prompt.choose_vendor.return_value = elsewhere
        result = obtain_vendor(self.source, self.root, self.work, prompt)
        self.assertEqual(result, self.work / self.source['filename'])
        self.assertTrue(elsewhere.exists())

    def test_download_choice_immediately_fetches_to_working_folder(self):
        prompt = Mock()
        prompt.choose_vendor.return_value = 'download'
        with patch('builder.downloads.fetch', return_value=self.work / self.source['filename']) as download:
            obtain_vendor(self.source, self.root, self.work, prompt)
            download.assert_called_once_with(self.source, self.work)

    def test_cancel_never_downloads(self):
        prompt = Mock()
        prompt.choose_vendor.side_effect = Cancelled('cancelled')
        with patch('builder.downloads.fetch') as download, self.assertRaises(Cancelled):
            obtain_vendor(self.source, self.root, self.work, prompt)
        download.assert_not_called()

    def test_bad_download_does_not_become_cached_archive(self):
        response = io.BytesIO(b'wrong content')
        response.url = self.source['url']
        response.headers = {}
        with patch('urllib.request.urlopen', return_value=response), self.assertRaises(ValueError):
            fetch(self.source, self.work)
        self.assertFalse((self.work / self.source['filename']).exists())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_extracts_only_setup_without_archive_paths(self):
        archive = self.root / 'vendor.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            zipped.writestr('Product/Setup.EXE', b'MZ test')
            zipped.writestr('../../outside', b'not extracted')
        extracted = self.work / 'vendor'
        result = extract_setup(archive, extracted, hashlib.sha256(b'MZ test').hexdigest())
        self.assertEqual(result.read_bytes(), b'MZ test')
        self.assertEqual(list(extracted.iterdir()), [result])

    def test_tar_rejects_traversal_and_links(self):
        for name, kind in [('../outside', tarfile.REGTYPE), ('inside/link', tarfile.SYMTYPE)]:
            with self.subTest(name=name):
                archive = self.root / 'source.tar'
                with tarfile.open(archive, 'w') as tar:
                    entry = tarfile.TarInfo(name);entry.type = kind
                    tar.addfile(entry)
                with self.assertRaises(ValueError):
                    extract_tar(archive, self.work / 'unpacked')

    def test_kdialog_buttons_map_to_locate_download_cancel(self):
        prompt = Prompt(no_gui=True)
        prompt.kdialog = '/test/kdialog'
        with patch('subprocess.run', return_value=Mock(returncode=1)) as run:
            self.assertEqual(prompt.choose_vendor(self.source['url']), 'download')
            self.assertIn('Download', run.call_args.args[0])
        with patch('subprocess.run', return_value=Mock(returncode=0)), patch.object(prompt, 'locate_zip', return_value=self.root / 'vendor.zip') as locate:
            self.assertEqual(prompt.choose_vendor(self.source['url']), self.root / 'vendor.zip')
            locate.assert_called_once()
        with patch('subprocess.run', return_value=Mock(returncode=2)), self.assertRaises(Cancelled):
            prompt.choose_vendor(self.source['url'])

    def test_rootfs_absolute_symlinks_remain_inside_working_directory(self):
        from builder.portable import extract_rootfs
        archive = self.root/'rootfs.tar'
        with tarfile.open(archive, 'w') as tar:
            entry = tarfile.TarInfo('etc/mtab')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '/proc/mounts'
            tar.addfile(entry)
        target = self.work/'rootfs'
        extract_rootfs(archive, target)
        self.assertEqual((target/'etc/mtab').readlink(), Path('../proc/mounts'))
        self.assertTrue((target/'etc/mtab').resolve().is_relative_to(target))

    def test_rootfs_traversal_is_rejected_and_partial_extraction_removed(self):
        from builder.portable import extract_rootfs
        archive = self.root/'rootfs.tar'
        with tarfile.open(archive, 'w') as tar:
            tar.addfile(tarfile.TarInfo('../escape'))
        target = self.work/'rootfs'
        with self.assertRaises(tarfile.FilterError):
            extract_rootfs(archive, target)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
