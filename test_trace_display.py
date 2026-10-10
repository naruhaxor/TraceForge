import csv
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W

from app import MainWindow
from acquisition import Acquisition
from core import CaptureReader, CaptureWriter, FrameBuffer, SampleBuffer, to_volts


class TraceDisplayTests(unittest.TestCase):
    def setUp(self):
        self.application = W.QApplication.instance() or W.QApplication([])
        self.application.setQuitOnLastWindowClosed(False)
        self.window = MainWindow()
        self.window.timer.stop()
        self.window.readout_timer.stop()
        self.worker = Mock()
        self.worker.isRunning.return_value = True
        self.worker.stop_event = threading.Event()
        self.worker.channels = list(range(8))
        self.worker.mode = 'roll'
        self.worker.ns_per_div = 100000
        self.worker.last_received = time.monotonic()
        self.window.worker = self.worker
        self.metadata = {'channel_order': list(range(1, 9)), 'zero_offsets_counts': [2000]*8,
                         'volts_per_count': [.01]*8, 'sample_rate_nominal_hz': 440,
                         'probe_factors_at_record_start': [1]*8, 'acquisition_mode': 'roll'}
        self.window.metadata = self.metadata
        self.raw = np.tile(np.arange(128, dtype='<u2')[:, None], (1, 8)) + 2000
        self.window.buffer.append(self.raw)
        self.window.displayed = (0, self.raw.copy())
        self.window.draw(*self.window.displayed)
        self.window.first_data = False
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        def cleanup():
            self.window.worker = None
            self.window.close()
            self.application.processEvents()
        self.addCleanup(cleanup)

    def test_hiding_and_showing_channels_preserves_active_recording_and_clock(self):
        window = self.window
        window.recording_active = window.recording_requested = True
        window.recording_part = 1
        window.last_recording = Path(self.temp.name) / 'Running.hscope'
        buffer = window.buffer
        configuration = window.capture_configuration()
        with patch.object(window, 'start_acquisition') as restart:
            window.channel_checks[0].setChecked(False)
            window.select_channels([])
            window.buffer.append(self.raw)
            window.refresh()
            window.select_channels([0, 2, 7])
            window.channel_checks[0].setChecked(False)
            window.channel_checks[0].setChecked(True)
            self.assertIs(window.worker, self.worker)
            self.assertIs(window.buffer, buffer)
            self.assertEqual(window.buffer.total, 256)
            self.assertEqual(window.capture_configuration(), configuration)
            self.assertTrue(window.recording_active and window.recording_requested)
            self.assertFalse(window.recording_pending or window.restart_pending)
            self.assertFalse(window.reconfigure_timer.isActive())
            self.assertEqual(window.recording_part, 1)
            self.assertEqual(window.last_recording.name, 'Running.hscope')
            restart.assert_not_called()
            self.worker.stop.assert_not_called()
            self.worker.stop_recording.assert_not_called()
            self.worker.start_recording.assert_not_called()
        expected = np.tile(to_volts(self.raw, self.metadata)[:, 0], 2) + window.channel_offsets[0]
        np.testing.assert_allclose(window.curves[0].getData()[1], expected)

    def test_startup_and_newly_shown_channels_stack_automatically(self):
        window = self.window
        offsets = window.channel_offsets.copy()
        self.assertTrue(np.all(np.diff(offsets) < 0))
        window.select_channels([0, 2])
        window.channel_checks[6].setChecked(True)
        traces = [window.curves[i].getData()[1] for i in (0, 2, 6)]
        self.assertGreater(np.min(traces[0]), np.max(traces[1]))
        self.assertGreater(np.min(traces[1]), np.max(traces[2]))
        window.position_controls[2].setValue(9)
        window.buffer.append(self.raw)
        window.refresh()
        self.assertEqual(window.channel_offsets[2], 9)
        self.worker.stop.assert_not_called()

    def test_offsets_and_drag_preserve_voltage_samples_readouts_and_exports(self):
        window = self.window
        measured = to_volts(self.raw, self.metadata)
        old_readout = window.channel_values[2].text()
        old_time = window.curves[2].getData()[0].copy()
        window.position_controls[2].setValue(12.5)
        window.channel_baselines[2].setValue(-6.25)
        self.assertEqual(window.position_controls[2].value(), -6.25)
        np.testing.assert_array_equal(window.curves[2].getData()[0], old_time)
        np.testing.assert_allclose(window.curves[2].getData()[1], measured[:, 2]-6.25)
        np.testing.assert_array_equal(window.buffer.snapshot()[1], self.raw)
        self.assertEqual(window.channel_values[2].text(), old_readout)
        path = Path(self.temp.name) / 'voltages.csv'
        window.export_view(path)
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        np.testing.assert_allclose([float(row['CH3_V']) for row in rows], measured[:, 2])
        metadata = json.loads(path.with_suffix('.csv.metadata.json').read_text())
        self.assertFalse(metadata['exported_voltage_includes_display_offsets'])
        self.assertEqual(metadata['display_offsets_v']['CH3'], -6.25)
        self.worker.stop.assert_not_called()

    def test_position_drag_keeps_view_stable_as_new_data_arrives(self):
        window = self.window
        window.plot.setYRange(-10, 10, padding=0)
        before = window.plot.viewRange()[1]
        window.channel_baselines[0].setValue(4)
        self.assertEqual(window.voltage_span.currentData(), 'manual')
        window.buffer.append(self.raw)
        window.refresh()
        np.testing.assert_allclose(window.plot.viewRange()[1], before)
        self.assertEqual(window.channel_offsets[0], 4)

    def test_graph_readouts_follow_latest_samples_without_waiting_for_plot_redraw(self):
        window = self.window
        window.name_controls[2].setText('Cam {bank 1}')
        position = window.channel_baselines[2].value()
        for buffer_type in (SampleBuffer, FrameBuffer):
            with self.subTest(buffer=buffer_type.__name__):
                window.buffer = buffer_type()
                for counts, expected in ((2133, '1.330 V'), (1841, '-1.590 V')):
                    window.buffer.append(np.full((1, 8), counts, dtype='<u2'))
                    window.last_readout_key = None
                    window.update_latest_readouts()
                    self.assertEqual(window.channel_values[2].text(), expected)
                    self.assertEqual(window.channel_baselines[2].label.textItem.toPlainText(), f'Cam {{bank 1}} · {expected}')
                    self.assertEqual(window.channel_baselines[2].value(), position)
        np.testing.assert_array_equal(window.displayed[1], self.raw)
        self.worker.stop.assert_not_called()

    def test_graph_readout_rename_probe_and_visibility_keep_the_measured_voltage(self):
        window = self.window
        window.update_latest_readouts()
        window.name_controls[2].setText('Cam <A> {bank}')
        label = window.channel_baselines[2].label
        self.assertEqual(label.textItem.toPlainText(), 'Cam <A> {bank} · 1.270 V')
        window.probes[2].setCurrentIndex(1)
        window.update_latest_readouts()
        self.assertEqual(label.textItem.toPlainText(), 'Cam <A> {bank} · 12.700 V')
        window.channel_baselines[2].setValue(-8)
        self.assertEqual(label.textItem.toPlainText(), 'Cam <A> {bank} · 12.700 V')
        window.channel_checks[2].setChecked(False)
        window.buffer.append(np.full((1, 8), 1900, dtype='<u2'))
        window.channel_checks[2].setChecked(True)
        self.assertEqual(label.textItem.toPlainText(), 'Cam <A> {bank} · -10.000 V')
        window.name_controls[2].clear()
        self.assertEqual(label.textItem.toPlainText(), 'CH3 · -10.000 V')

    def test_hold_freezes_graph_readout_and_resume_uses_newest_sample(self):
        window = self.window
        window.toggle_hold(True)
        label = window.channel_baselines[0].label
        self.assertEqual(label.textItem.toPlainText(), 'CH1 · 1.270 V')
        window.buffer.append(np.full((1, 8), 1841, dtype='<u2'))
        window.update_latest_readouts()
        window.refresh()
        self.assertEqual(label.textItem.toPlainText(), 'CH1 · 1.270 V')
        window.toggle_hold(False)
        self.assertEqual(label.textItem.toPlainText(), 'CH1 · -1.590 V')

    def test_noise_filter_cycles_preserve_samples_recording_and_export_precision(self):
        window = self.window
        window.metadata['volts_per_count'] = [.001]*8
        raw = np.tile(np.array([2000, 3234], dtype='<u2')[:, None], (1, 8))
        window.buffer = SampleBuffer()
        window.buffer.append(raw)
        window.displayed = (0, raw.copy())
        window.last_readout_key = None
        window.draw(*window.displayed)
        window.recording_active = window.recording_requested = True
        curves = [curve.getData()[1].copy() for curve in window.curves]
        self.assertEqual(window.channel_values[0].text(), '1.234 V')
        for mode, value, stats in [('LOW', '1.23 V', 'min 0.00   max 1.23'),
                                   ('HIGH', '1.2 V', 'min 0.0   max 1.2'),
                                   ('OFF', '1.234 V', 'min 0.000   max 1.234')]:
            window.noise_filter_button.click()
            self.assertEqual(window.noise_filter_button.text(), 'Noise filter: ' + mode)
            self.assertEqual(window.channel_values[0].text(), value)
            self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'CH1 · ' + value)
            self.assertEqual(window.channel_stats[0].text(), stats)
            for curve, original in zip(window.curves, curves):
                np.testing.assert_array_equal(curve.getData()[1], original)
            np.testing.assert_array_equal(window.buffer.snapshot()[1], raw)
            self.assertTrue(window.recording_active and window.recording_requested)
            self.assertFalse(window.restart_pending or window.reconfigure_timer.isActive())
            path = Path(self.temp.name) / f'{mode}.csv'
            window.export_view(path)
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(float(rows[-1]['CH1_V']), 1.234)
        self.worker.stop.assert_not_called()
        self.worker.start_recording.assert_not_called()
        self.worker.stop_recording.assert_not_called()

    def test_noise_filter_rounds_new_samples_and_suppresses_negative_zero(self):
        window = self.window
        window.metadata['volts_per_count'] = [.001]*8
        window.noise_filter_button.click()
        for counts, expected in [(3236, '1.24 V'), (764, '-1.24 V'), (1999, '0.00 V')]:
            window.buffer.append(np.full((1, 8), counts, dtype='<u2'))
            window.update_latest_readouts()
            self.assertEqual(window.channel_values[0].text(), expected)
            self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'CH1 · ' + expected)
        window.noise_filter_button.click()
        self.assertEqual(window.channel_values[0].text(), '0.0 V')
        window.noise_filter_button.click()
        self.assertEqual(window.channel_values[0].text(), '-0.001 V')

    def test_noise_filter_formats_held_and_stopped_values_without_resuming(self):
        window = self.window
        window.toggle_hold(True)
        window.buffer.append(np.full((1, 8), 1841, dtype='<u2'))
        window.noise_filter_button.click()
        self.assertTrue(window.held)
        self.assertEqual(window.channel_values[0].text(), '1.27 V')
        window.noise_filter_button.click()
        self.assertEqual(window.channel_values[0].text(), '1.3 V')
        window.toggle_hold(False)
        self.assertEqual(window.channel_values[0].text(), '-1.6 V')
        window.worker = None
        window.noise_filter_button.click()
        self.assertEqual(window.channel_values[0].text(), '-1.590 V')
        self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'CH1 · -1.590 V')

    def test_noise_filter_keeps_empty_and_hidden_channels_empty(self):
        fresh = MainWindow()
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.noise_filter_button.text(), 'Noise filter: OFF')
        fresh.noise_filter_button.click()
        self.assertEqual(fresh.channel_values[0].text(), '— V')
        self.assertEqual(fresh.channel_baselines[0].label.textItem.toPlainText(), 'CH1 · — V')
        window = self.window
        window.channel_checks[2].setChecked(False)
        window.noise_filter_button.click()
        self.assertEqual(window.channel_values[2].text(), 'OFF')
        self.assertEqual(window.channel_stats[2].text(), 'hidden · still captured')
        window.channel_checks[2].setChecked(True)
        self.assertEqual(window.channel_values[2].text(), '1.27 V')

    def test_graph_filter_rounds_before_offsets_and_restores_original_without_moving_view(self):
        window = self.window
        window.metadata['volts_per_count'] = [.001]*8
        window.draw(*window.displayed)
        window.position_controls[0].setValue(.007)
        window.plot.setXRange(.05, .15, padding=0)
        original_x, original_y = (data.copy() for data in window.curves[0].getOriginalDataset())
        original_view = window.plot.viewRange()
        self.assertFalse(window.graph_filter_button.isChecked())
        window.graph_filter_button.click()
        np.testing.assert_array_equal(window.curves[0].getOriginalDataset()[1], original_y)
        for decimals in (2, 1):
            window.noise_filter_button.click()
            expected = np.round(to_volts(self.raw, window.metadata)[:, 0], decimals) + .007
            np.testing.assert_allclose(window.curves[0].getOriginalDataset()[1], expected)
            np.testing.assert_array_equal(window.curves[0].getOriginalDataset()[0], original_x)
            np.testing.assert_allclose(window.plot.viewRange(), original_view)
            np.testing.assert_array_equal(window.buffer.snapshot()[1], self.raw)
            np.testing.assert_array_equal(window.view_samples[1], self.raw)
        window.graph_filter_button.click()
        np.testing.assert_array_equal(window.curves[0].getOriginalDataset()[1], original_y)
        window.graph_filter_button.click()
        window.noise_filter_button.click()
        np.testing.assert_array_equal(window.curves[0].getOriginalDataset()[1], original_y)
        self.worker.stop.assert_not_called()
        self.assertFalse(window.restart_pending or window.reconfigure_timer.isActive())

    def test_graph_filter_applies_to_new_fast_frames_and_preserves_frame_time(self):
        window = self.window
        window.metadata.update(acquisition_mode='burst', sample_rate_nominal_hz=300000)
        window.configure_time_controls('burst')
        window.buffer = FrameBuffer()
        window.buffer.append(self.raw)
        window.noise_filter_button.click()
        window.noise_filter_button.click()
        window.graph_filter_button.click()
        window.displayed = window.buffer.snapshot()
        window.draw(*window.displayed)
        original_x = window.curves[0].getOriginalDataset()[0].copy()
        np.testing.assert_allclose(window.curves[0].getOriginalDataset()[1],
            np.round(to_volts(self.raw, window.metadata)[:, 0], 1) + window.channel_offsets[0])
        newer = self.raw + 1
        window.buffer.append(newer)
        window.refresh()
        np.testing.assert_array_equal(window.curves[0].getOriginalDataset()[0], original_x)
        np.testing.assert_allclose(window.curves[0].getOriginalDataset()[1],
            np.round(to_volts(newer, window.metadata)[:, 0], 1) + window.channel_offsets[0])
        np.testing.assert_array_equal(window.buffer.snapshot()[1], newer)
        window.toggle_hold(True)
        window.graph_filter_button.click()
        self.assertTrue(window.held)
        np.testing.assert_allclose(window.curves[0].getOriginalDataset()[1],
            to_volts(newer, window.metadata)[:, 0] + window.channel_offsets[0])

    def test_graph_filter_during_capture_leaves_every_recorded_sample_unfiltered(self):
        window = self.window
        window.buffer = SampleBuffer()
        acquisition = Acquisition(window.buffer)
        window.worker = acquisition
        path = Path(self.temp.name) / 'Unfiltered recording.hscope'
        acquisition.start_recording(path, [1]*8)
        acquisition.ready.connect(lambda meta: setattr(window, 'metadata', meta))
        scope = Mock()
        scope._dev = None
        scope._device_response_blocks = {}
        scope.device_metadata = {}
        scope.get_zero_offsets.return_value = {1.: [2000]*8}
        def stream(**_):
            yield {ch: [2123, 2124] for ch in range(8)}
            window.displayed = window.buffer.snapshot()
            window.draw(*window.displayed)
            window.noise_filter_button.click()
            window.noise_filter_button.click()
            window.graph_filter_button.click()
            np.testing.assert_allclose(window.curves[0].getOriginalDataset()[1], 1.2 + window.channel_offsets[0])
            yield {ch: [2126, 2127] for ch in range(8)}
        scope.request_samples_roll_mode.side_effect = stream
        errors, events = [], []
        acquisition.failed.connect(errors.append)
        acquisition.recording.connect(lambda active, _: events.append(active))
        with patch('acquisition.Hantek1008Raw', return_value=scope):
            acquisition.run()
        self.assertEqual(errors, [])
        self.assertEqual(events, [True, False])
        self.assertEqual(scope.connect.call_count, 1)
        reader = CaptureReader(path)
        try:
            expected = np.tile(np.array([2123, 2124, 2126, 2127], dtype='<u2')[:, None], (1, 8))
            np.testing.assert_array_equal(reader.window(0, 4)[1], expected)
            self.assertTrue(reader.metadata['complete'])
            self.assertNotIn('display_graph_filter', reader.metadata)
        finally:
            reader.close()
        window.displayed = window.buffer.snapshot()
        window.draw(*window.displayed)
        exported = Path(self.temp.name) / 'Unfiltered export.csv'
        window.export_view(exported)
        with exported.open() as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([float(row['CH1_V']) for row in rows], [1.23, 1.24, 1.26, 1.27])
        metadata = json.loads(exported.with_suffix('.csv.metadata.json').read_text())
        self.assertTrue(metadata['display_graph_filter'])
        self.assertFalse(metadata['exported_voltage_includes_graph_filter'])

    def test_left_triangle_mouse_drag_moves_only_one_trace(self):
        window = self.window
        window.show()
        self.application.processEvents()
        window.plot.setYRange(-10, 10, padding=0)
        self.application.processEvents()
        original = window.channel_offsets.copy()
        view = window.plot.getViewBox()
        viewport = window.plot.viewport()
        x0, x1 = window.plot.viewRange()[0]
        points = [QtCore.QPointF(window.plot.mapFromScene(view.mapViewToScene(QtCore.QPointF(x0+(x1-x0)*.019, y))))
                  for y in (original[0], original[0]+2)]
        events = [(QtCore.QEvent.Type.MouseButtonPress, points[0], QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton),
                  (QtCore.QEvent.Type.MouseMove, points[1], QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.LeftButton),
                  (QtCore.QEvent.Type.MouseButtonRelease, points[1], QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.NoButton)]
        for kind, position, button, buttons in events:
            event = QtGui.QMouseEvent(kind, position, QtCore.QPointF(viewport.mapToGlobal(position.toPoint())), button, buttons, QtCore.Qt.KeyboardModifier.NoModifier)
            W.QApplication.sendEvent(viewport, event)
            self.application.processEvents()
        self.assertAlmostEqual(window.channel_offsets[0]-original[0], 2, delta=.1)
        np.testing.assert_array_equal(window.channel_offsets[1:], original[1:])
        self.worker.stop.assert_not_called()

    def test_stack_separates_visible_traces_and_reset_preserves_amplitude(self):
        window = self.window
        window.select_channels([0, 2, 7])
        window.stack_traces()
        traces = [window.curves[i].getData()[1] for i in (0, 2, 7)]
        self.assertGreater(np.min(traces[0]), np.max(traces[1]))
        self.assertGreater(np.min(traces[1]), np.max(traces[2]))
        for trace in traces:
            self.assertAlmostEqual(float(np.ptp(trace)), 1.27)
        window.reset_trace_positions()
        for i in (0, 2, 7):
            np.testing.assert_allclose(window.curves[i].getData()[1], to_volts(self.raw, self.metadata)[:, i])
        np.testing.assert_array_equal(window.channel_offsets, np.zeros(8))
        self.worker.stop.assert_not_called()

    def test_replay_maps_positions_to_physical_channels_and_keeps_file_unchanged(self):
        window = self.window
        window.worker = None
        path = Path(self.temp.name) / 'Old selective recording.hscope'
        metadata = dict(self.metadata, channel_order=[1, 3, 7], zero_offsets_counts=[2000]*3,
                        volts_per_count=[.01]*3, probe_factors_at_record_start=[1]*3)
        writer = CaptureWriter(path, metadata, 0)
        writer.append(0, self.raw[:, [0, 2, 6]], 1)
        writer.close()
        original = path.read_bytes()
        window.open_capture(path)
        self.assertIsNone(window.last_error)
        window.position_controls[6].setValue(8)
        window.channel_checks[2].setChecked(False)
        self.assertFalse(window.channel_baselines[2].isVisible())
        self.assertFalse(window.channel_checks[1].isEnabled())
        np.testing.assert_allclose(window.curves[6].getData()[1], to_volts(self.raw[:, [0, 2, 6]], metadata)[:, 2]+8)
        self.assertEqual(window.recorded_channels(), [1, 3, 7])
        self.assertEqual(window.channel_baselines[6].label.textItem.toPlainText(), 'CH7 · 1.270 V')
        window.draw(0, self.raw[:10, [0, 2, 6]])
        self.assertEqual(window.channel_baselines[6].label.textItem.toPlainText(), 'CH7 · 0.090 V')
        window.noise_filter_button.click()
        self.assertEqual(window.channel_baselines[6].label.textItem.toPlainText(), 'CH7 · 0.09 V')
        window.noise_filter_button.click()
        self.assertEqual(window.channel_baselines[6].label.textItem.toPlainText(), 'CH7 · 0.1 V')
        window.graph_filter_button.click()
        np.testing.assert_allclose(window.curves[6].getOriginalDataset()[1],
                                  np.round(to_volts(self.raw[:10, [0, 2, 6]], metadata)[:, 2], 1) + 8)
        self.assertFalse(window.channel_baselines[2].isVisible())
        window.graph_filter_button.click()
        np.testing.assert_allclose(window.curves[6].getOriginalDataset()[1],
                                  to_volts(self.raw[:10, [0, 2, 6]], metadata)[:, 2] + 8)
        self.assertEqual(path.read_bytes(), original)

    def test_connect_acquires_all_eight_even_when_all_traces_hidden(self):
        window = self.window
        window.worker = None
        window.select_channels([])
        worker = Mock()
        worker.isRunning.return_value = False
        worker.stop_event = threading.Event()
        with patch('app.usb_configured', return_value=True), patch.object(window, 'check_device', return_value=True), patch('app.Acquisition', return_value=worker) as factory:
            window.start_acquisition()
        self.assertEqual(factory.call_args.kwargs['channels'], list(range(8)))
        self.assertEqual(window.buffer.channel_count, 8)
        self.assertTrue(all(not check.isChecked() for check in window.channel_checks))
        worker.start.assert_called_once()

    def test_new_acquisition_clears_previous_graph_voltage(self):
        window = self.window
        window.name_controls[0].setText('Crank')
        window.worker = None
        with patch('app.usb_configured', return_value=True), patch.object(window, 'check_device', return_value=True), patch('app.Acquisition'):
            window.start_acquisition()
        self.assertEqual(window.channel_values[0].text(), '— V')
        self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'Crank · — V')
        window.noise_filter_button.click()
        self.assertEqual(window.channel_baselines[0].label.textItem.toPlainText(), 'Crank · — V')


if __name__ == '__main__':
    unittest.main()
