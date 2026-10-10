import datetime as dt
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import paths


class RecordingPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for mocked in (patch.dict(os.environ, {'HANTEK_DATA_DIR': str(self.root / 'app-data')}),
                       patch.object(Path, 'home', return_value=self.root)):
            mocked.start()
            self.addCleanup(mocked.stop)
        os.environ.pop('HANTEK_RECORDINGS_DIR', None)

    def test_default_named_layout_without_overwrite(self):
        base = self.root / 'Documents' / 'Traceforge'
        self.assertEqual(paths.recordings_directory(), base)
        instant = dt.datetime(2026, 10, 9, 13, 30, 40, 123456)
        first = paths.new_recording_path('Battery check.hscope', instant)
        self.assertEqual(first, base / '2026-10-09' / 'Battery check.hscope')
        self.assertFalse(base.exists())
        first.parent.mkdir(parents=True)
        first.write_bytes(b'earlier recording stays intact')
        with self.assertRaises(FileExistsError):
            paths.new_recording_path('Battery check', instant)
        second = paths.new_recording_path('Battery check', instant, part=2)
        self.assertEqual(second.name, 'Battery check-part-02.hscope')
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b'earlier recording stays intact')

    def test_filename_validation_and_extension(self):
        for invalid in ['', ' ', '.', '..', '.hscope', '../escape', '/absolute', 'nested/file', 'nested\\file', 'bad\x00name', 'bad\nname', 'a'*201]:
            with self.subTest(name=invalid), self.assertRaises(ValueError):
                paths.new_recording_path(invalid)
        self.assertEqual(paths.recording_filename('  AA battery.HSCOPE '), 'AA battery.hscope')
        self.assertEqual(paths.recording_filename('Café voltage'), 'Café voltage.hscope')
        self.assertFalse(paths.recordings_directory().exists())

    def test_custom_location_persists_and_keeps_old_files(self):
        first = paths.new_recording_path('Original')
        first.parent.mkdir(parents=True)
        first.write_bytes(b'preserved')
        custom = self.root / 'External drive' / 'Customer captures'
        paths.set_recordings_directory(custom)
        self.assertEqual(paths.recordings_directory(), custom)
        later = paths.new_recording_path('New capture')
        self.assertTrue(later.is_relative_to(custom))
        self.assertEqual(first.read_bytes(), b'preserved')
        paths.set_recordings_directory(paths.default_recordings_directory())
        self.assertEqual(paths.recordings_directory(), self.root / 'Documents' / 'Traceforge')

    def live_window(self):
        from PySide6 import QtWidgets as W
        from app import MainWindow
        self.application = W.QApplication.instance() or W.QApplication([])
        self.application.setQuitOnLastWindowClosed(False)
        window = MainWindow()
        window.timer.stop()
        window.readout_timer.stop()
        worker = Mock()
        worker.isRunning.return_value = True
        worker.stop_event = threading.Event()
        window.worker = worker
        window.metadata = {'channel_order': list(range(1, 9)), 'sample_rate_nominal_hz': 440,
                           'zero_offsets_counts': [2000]*8, 'volts_per_count': [.01]*8}
        def cleanup():
            window.worker = None
            window.close()
            self.application.processEvents()
        self.addCleanup(cleanup)
        return window, worker

    def record_dialog(self, window, name, accept=True, during=None):
        from PySide6 import QtCore, QtWidgets as W
        result = {}
        def interact():
            dialog = self.application.activeModalWidget()
            try:
                dialog.findChild(W.QLineEdit, 'recordingName').setText(name)
                controls = dialog.findChild(W.QDialogButtonBox)
                start = controls.button(W.QDialogButtonBox.StandardButton.Save)
                result['enabled'] = start.isEnabled()
                result['destination'] = dialog.findChild(W.QLabel, 'recordingDestination').text()
                result['error'] = dialog.findChild(W.QLabel, 'recordingNameError').text()
                if during:
                    during()
                if accept and start.isEnabled():
                    start.click()
                else:
                    dialog.reject()
            except Exception as exc:
                result['exception'] = exc
                dialog.reject()
        QtCore.QTimer.singleShot(0, interact)
        window.toggle_record()
        if 'exception' in result:
            raise result['exception']
        return result

    def test_live_view_cancel_creates_nothing(self):
        window, worker = self.live_window()
        result = self.record_dialog(window, 'Cancelled recording', accept=False)
        self.assertTrue(result['enabled'])
        self.assertTrue(window.active())
        self.assertFalse(window.recording_pending)
        self.assertIsNone(window.last_recording)
        worker.start_recording.assert_not_called()
        worker.stop.assert_not_called()
        self.assertFalse(paths.recordings_directory().exists())

    def test_named_record_starts_saving_and_finish_does_not_prompt(self):
        from core import CaptureReader, CaptureWriter
        window, worker = self.live_window()
        writers = []
        def start(path, factors, names):
            writer = CaptureWriter(path, dict(window.metadata, probe_factors_at_record_start=factors, channel_names=names), 0)
            writer.append(0, np.full((4, 8), 2159, dtype='<u2'), 10)
            writer.flush()
            writers.append(writer)
            self.addCleanup(writer.close)
            window.on_recording(True, path)
        def stop():
            writers[-1].close()
            window.on_recording(False, str(writers[-1].path))
        worker.start_recording.side_effect = start
        worker.stop_recording.side_effect = stop
        result = self.record_dialog(window, 'Battery check.hscope')
        self.assertTrue(result['enabled'])
        self.assertIn('Battery check.hscope', result['destination'])
        self.assertEqual(window.last_recording, paths.recordings_directory() / dt.date.today().isoformat() / 'Battery check.hscope')
        self.assertTrue(window.last_recording.is_file())
        self.assertTrue(window.recording_active)
        with patch.object(window, 'prompt_recording_path', side_effect=AssertionError('Finish must not prompt')):
            window.toggle_record()
        self.assertFalse(window.recording_active)
        self.assertTrue(window.active())
        reader = CaptureReader(window.last_recording)
        try:
            self.assertTrue(reader.metadata['complete'])
            self.assertEqual(reader.count, 4)
        finally:
            reader.close()

    def test_invalid_and_existing_names_cannot_start(self):
        window, worker = self.live_window()
        path = paths.new_recording_path('Existing')
        path.parent.mkdir(parents=True)
        path.write_bytes(b'preserve me')
        for name in ['', '../escape', 'Existing']:
            result = self.record_dialog(window, name)
            self.assertFalse(result['enabled'])
        worker.start_recording.assert_not_called()
        self.assertEqual(path.read_bytes(), b'preserve me')

    def test_acquisition_stopping_during_prompt_cancels_start(self):
        window, worker = self.live_window()
        self.record_dialog(window, 'Disconnected', during=worker.stop_event.set)
        worker.start_recording.assert_not_called()
        self.assertFalse(paths.recordings_directory().exists())

    def test_configuration_resume_keeps_name_and_skips_existing_parts(self):
        window, worker = self.live_window()
        window.recording_name = 'Battery check'
        window.recording_part = 1
        occupied = paths.new_recording_path('Battery check', part=2)
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b'earlier part')
        window.resume_recording = True
        with patch.object(window, 'prompt_recording_path', side_effect=AssertionError('Resume must not prompt')):
            window.device_ready(window.metadata)
        expected = paths.new_recording_path('Battery check', part=3)
        worker.start_recording.assert_called_once_with(str(expected), [1]*8, window.channel_names)
        self.assertEqual(window.recording_part, 3)
        self.assertEqual(occupied.read_bytes(), b'earlier part')

    def test_unusable_location_does_not_replace_saved_choice(self):
        good = self.root / 'Working'
        paths.set_recordings_directory(good)
        file = self.root / 'not-a-folder'
        file.write_bytes(b'keep')
        with self.assertRaises(OSError):
            paths.set_recordings_directory(file)
        self.assertEqual(paths.recordings_directory(), good)
        with self.assertRaises(ValueError):
            paths.set_recordings_directory('relative/path')
        self.assertEqual(paths.recordings_directory(), good)

    def test_gui_save_cancel_and_default(self):
        from PySide6 import QtCore, QtWidgets as W
        from app import MainWindow
        application = W.QApplication.instance() or W.QApplication([])
        application.setQuitOnLastWindowClosed(False)
        window = MainWindow()
        old = self.root / 'old-recording.hscope'
        old.write_bytes(b'current recording')
        window.last_recording = old
        def choose(folder, accept):
            def interact():
                dialog = application.activeModalWidget()
                dialog.findChild(W.QLineEdit, 'recordingFolderPath').setText(str(folder))
                buttons = dialog.findChild(W.QDialogButtonBox)
                buttons.button(W.QDialogButtonBox.StandardButton.Save if accept else W.QDialogButtonBox.StandardButton.Cancel).click()
            QtCore.QTimer.singleShot(0, interact)
            window.choose_recording_directory()
        try:
            custom = self.root / 'New folder'
            choose(custom, True)
            self.assertEqual(paths.recordings_directory(), custom)
            self.assertIn(str(custom), window.recording_location_label.text())
            self.assertEqual(window.last_recording, old)
            self.assertEqual(old.read_bytes(), b'current recording')
            choose(self.root / 'Cancelled', False)
            self.assertEqual(paths.recordings_directory(), custom)
        finally:
            window.close()
            application.processEvents()


if __name__ == '__main__':
    unittest.main()
