import csv
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6 import QtWidgets as W

from acquisition import Acquisition
from app import MainWindow
from channel_names import MAX_NAME_LENGTH, clean_name, normalize_names
from core import CaptureReader, CaptureWriter, SampleBuffer
from reference_overlay import ReferenceDialog


class ChannelNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, HANTEK_DATA_DIR=str(self.root/'settings'))
        env.start()
        self.addCleanup(env.stop)
        self.application = W.QApplication.instance() or W.QApplication([])
        self.application.setQuitOnLastWindowClosed(False)
        self.meta = dict(channel_order=[1, 3], zero_offsets_counts=[2000]*2,
                         volts_per_count=[.01]*2, sample_rate_nominal_hz=440,
                         probe_factors_at_record_start=[1, 1], acquisition_mode='roll')
        self.raw = np.full((20, 2), 2133, dtype='<u2')

    def window(self):
        window = MainWindow()
        window.timer.stop()
        window.readout_timer.stop()
        def cleanup():
            window.worker = None
            window.close()
            self.application.processEvents()
        self.addCleanup(cleanup)
        return window

    def recording(self, names=None):
        path = self.root/f'reference-{len(list(self.root.glob("*.hscope")))}.hscope'
        meta = dict(self.meta)
        if names is not None:
            meta['channel_names'] = names
        writer = CaptureWriter(path, meta, 0)
        writer.append(0, self.raw, 1)
        writer.close()
        return path

    def test_names_are_session_only_and_invalid_metadata_falls_back(self):
        self.assertEqual(normalize_names({'1': '  Crank  sensor ', '3': 'Cám\nB', '4': ' ', '9': 'invalid', '2': 15}),
                         {'1': 'Crank sensor', '3': 'Cám B'})
        self.assertEqual(len(clean_name('x'*100)), MAX_NAME_LENGTH)
        self.assertEqual(normalize_names(['not a mapping']), {})
        window = self.window()
        window.name_controls[0].setText('Session crank')
        window.commit_channel_names()
        self.assertEqual(self.window().channel_names, {})
        self.assertFalse((self.root/'settings/channel-names.json').exists())

    def test_edit_updates_graph_immediately_and_blank_restores_physical_name(self):
        window = self.window()
        window.name_controls[0].setText('Crank {A} <test> & cam')
        self.assertEqual(window.channel_title(0, physical=True), 'CH1 - Crank {A} <test> & cam')
        self.assertEqual(window.curves[0].name(), 'Crank {A} <test> & cam')
        self.assertEqual(window.legend.getLabel(window.curves[0]).item.toPlainText(), 'Crank {A} <test> & cam')
        self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'Crank {A} <test> & cam · — V')
        window.set_channel_offset(0, 8)
        self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'Crank {A} <test> & cam · — V')
        window.commit_channel_names()
        restored = self.window()
        self.assertEqual(restored.name_controls[0].text(), '')
        window.name_controls[0].setText('   ')
        window.commit_channel_names()
        self.assertEqual(window.curves[0].name(), 'CH1')
        self.assertEqual(window.name_controls[0].text(), '')

    def test_rename_during_capture_preserves_clock_recording_and_samples(self):
        window = self.window()
        worker = Mock()
        worker.isRunning.return_value = True
        worker.stop_event = threading.Event()
        window.worker = worker
        window.metadata = dict(self.meta, channel_order=list(range(1, 9)), zero_offsets_counts=[2000]*8,
                               volts_per_count=[.01]*8, probe_factors_at_record_start=[1]*8)
        raw = np.full((20, 8), 2133, dtype='<u2')
        window.buffer.append(raw)
        window.displayed = (0, raw.copy())
        window.draw(*window.displayed)
        buffer, offsets = window.buffer, window.channel_offsets.copy()
        window.recording_active = window.recording_requested = True
        window.name_controls[2].setText('Intake cam')
        window.commit_channel_names()
        self.assertTrue(window.recording_active and window.recording_requested)
        self.assertIs(window.buffer, buffer)
        self.assertEqual(window.buffer.total, 20)
        np.testing.assert_array_equal(window.displayed[1], raw)
        np.testing.assert_array_equal(window.channel_offsets, offsets)
        self.assertEqual(window.channel_values[2].text(), '1.330 V')
        self.assertEqual(window.channel_baselines[2].label.textItem.toPlainText(), 'Intake cam · 1.330 V')
        worker.stop.assert_not_called()
        worker.stop_recording.assert_not_called()
        worker.start_recording.assert_not_called()
        worker.update_channel_names.assert_called_once_with({'3': 'Intake cam'})
        output = self.root/'view.csv'
        window.export_view(output)
        with output.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(float(rows[0]['CH3_V']), 1.33)
        self.assertEqual(json.loads(output.with_suffix('.csv.metadata.json').read_text())['channel_names'], {'3': 'Intake cam'})

    def test_recording_names_load_by_physical_channel_and_do_not_overwrite_live_names(self):
        window = self.window()
        window.name_controls[0].setText('Live crank')
        window.name_controls[2].setText('Live cam')
        path = self.recording({'1': 'Recorded crank', '3': 'Recorded cam'})
        original = path.read_bytes()
        window.open_capture(path)
        self.assertEqual(window.curves[0].name(), 'Recorded crank')
        self.assertEqual(window.curves[2].name(), 'Recorded cam')
        self.assertEqual(window.curves[1].name(), 'CH2')
        window.name_controls[2].setText('Temporary replay label')
        window.commit_channel_names()
        self.assertEqual(window.live_channel_names, {'1': 'Live crank', '3': 'Live cam'})
        window.reader.close()
        window.reader = None
        window.restore_live_visibility()
        self.assertEqual(window.curves[0].name(), 'Live crank')
        self.assertEqual(window.curves[2].name(), 'Live cam')
        self.assertEqual(path.read_bytes(), original)
        window.open_capture(self.recording())
        self.assertEqual(window.curves[0].name(), 'CH1')
        self.assertEqual(window.curves[2].name(), 'CH3')

    def test_named_reference_mapping_identifies_source_and_destination(self):
        window = self.window()
        window.name_controls[0].setText('Crank')
        dialog = ReferenceDialog(window.reference_overlay)
        dialog.add_file(str(self.recording({'3': 'Known cam'})))
        self.assertEqual(dialog.mappings[0].itemText(1), 'Graph CH1 - Crank')
        self.assertEqual(dialog.mapping_labels[2].text(), 'Reference CH3 - Known cam →')
        dialog.reject()

    def test_worker_saves_mid_recording_rename_without_splitting_capture(self):
        path = self.root/'worker.hscope'
        worker = Acquisition(SampleBuffer(100, 2), channels=[0, 2])
        names = {'1': 'Crank', '3': 'Cam'}
        worker.start_recording(path, [1, 1], names)
        names['1'] = 'Unsubmitted edit'
        scope = Mock()
        scope._dev = None
        scope._device_response_blocks = {}
        scope.device_metadata = {}
        scope.get_zero_offsets.return_value = {1.: [2000]*8}
        def stream(**_):
            yield {0: [2133, 2134], 2: [2159, 2160]}
            worker.update_channel_names({'1': 'Crank', '3': 'Intake cam'})
            yield {0: [2135, 2136], 2: [2161, 2162]}
            worker.update_channel_names({'1': 'Crank', '3': 'Cam bank 1'})
        scope.request_samples_roll_mode.side_effect = stream
        events, errors = [], []
        worker.recording.connect(lambda active, path: events.append((active, path)))
        worker.failed.connect(errors.append)
        with patch('acquisition.Hantek1008Raw', return_value=scope):
            worker.run()
        self.assertEqual(errors, [])
        self.assertEqual([active for active, _ in events], [True, False])
        with_reader = CaptureReader(path)
        try:
            self.assertEqual(with_reader.count, 4)
            self.assertTrue(with_reader.metadata['complete'])
            self.assertEqual(with_reader.metadata['channel_names'], {'1': 'Crank', '3': 'Cam bank 1'})
            np.testing.assert_array_equal(with_reader.window(0, 4)[1], [[2133, 2159], [2134, 2160], [2135, 2161], [2136, 2162]])
        finally:
            with_reader.close()
        self.assertEqual(scope.connect.call_count, 1)


if __name__ == '__main__':
    unittest.main()
