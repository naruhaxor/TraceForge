import base64
import csv
import json
import os
from pathlib import Path
import tempfile
import struct
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W

from app import MainWindow
from core import CaptureWriter
from reference_overlay import (ReferenceDialog, load_recording_reference, load_vendor_reference,
                               transformed_reference, recent_references, remember_reference,
                               load_image_reference, image_formats)


class ReferenceOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'Known good crank and cam.hscope'
        self.metadata = {'channel_order': [1, 3], 'zero_offsets_counts': [2000, 2000],
                         'volts_per_count': [.01, .01], 'sample_rate_nominal_hz': 1000,
                         'probe_factors_at_record_start': [1, 10], 'acquisition_mode': 'roll'}
        self.raw = np.column_stack((np.arange(100, dtype='<u2')+2000, np.arange(100, dtype='<u2')+2100))
        writer = CaptureWriter(self.path, self.metadata, 400)
        writer.append(400, self.raw, 1)
        writer.close()
        self.source_bytes = self.path.read_bytes()
        mock_env = patch.dict(os.environ, {'HANTEK_DATA_DIR': str(self.root/'settings')})
        mock_env.start()
        self.addCleanup(mock_env.stop)

    def test_recording_segment_uses_saved_probe_conversion_and_relative_time(self):
        data = load_recording_reference(self.path, .02, .03)
        self.assertEqual(data.channels, [1, 3])
        self.assertEqual(data.kind, 'recording')
        np.testing.assert_allclose(data.x, np.arange(30)/1000)
        np.testing.assert_allclose(data.values[:, 0], (self.raw[20:50, 0]-2000)*.01)
        np.testing.assert_allclose(data.values[:, 1], (self.raw[20:50, 1]-2000)*.1)
        self.assertEqual(self.path.read_bytes(), self.source_bytes)

    def test_frame_selection_keeps_frames_separate(self):
        path = self.root/'Frames.hscope'
        writer = CaptureWriter(path, dict(self.metadata, acquisition_mode='burst'), 0)
        writer.append(0, self.raw[:20], 1)
        writer.append(20, self.raw[20:40], 2)
        writer.close()
        data = load_recording_reference(path, frame=1)
        self.assertEqual(len(data.x), 20)
        self.assertIn('Frame 2/2', data.note)
        self.assertEqual(data.x[0], 0)
        self.assertAlmostEqual(data.values[0, 0], .2)
        with self.assertRaises(ValueError):
            load_recording_reference(path, frame=2)

    def test_invalid_and_oversized_segments_are_handled(self):
        for start, duration in [(-1, 1), (0, 0), (0, float('nan')), (2, 1)]:
            with self.assertRaises(ValueError):
                load_recording_reference(self.path, start, duration)
        with patch('reference_overlay.MAX_REFERENCE_ROWS', 12):
            data = load_recording_reference(self.path)
        self.assertEqual(len(data.x), 12)
        self.assertIn('shortened', data.note)
        self.assertEqual(self.path.read_bytes(), self.source_bytes)

    def test_group_alignment_keeps_channels_on_the_same_time_axis(self):
        data = load_recording_reference(self.path)
        original = data.values.copy()
        arrays = transformed_reference(data, [0, 6], [2, 0, 0, 0, 0, 0, -3, 0], anchor=5,
                                       shift_ms=12, stretch=2, gain=.5, vertical_shift=1)
        np.testing.assert_allclose(arrays[0][2], 5.012 + data.x*2)
        np.testing.assert_array_equal(arrays[0][2], arrays[1][2])
        np.testing.assert_allclose(arrays[0][3], data.values[:, 0]*.5+3)
        np.testing.assert_allclose(arrays[1][3], data.values[:, 1]*.5-2)
        np.testing.assert_array_equal(data.values, original)

    def test_vendor_examples_are_shape_only_and_explicitly_scaled(self):
        entry = {'traces': [{'npz_key': 'a'}, {'npz_key': 'b'}]}
        data = load_vendor_reference(entry, {'a': np.array([-5., 5., 0.]), 'b': np.array([4., 4., 4.])}, 'Vendor example')
        self.assertIn('SHAPE ONLY', data.note)
        arrays = transformed_reference(data, [0, 2], np.zeros(8), shape_duration=.1, shape_height=3)
        np.testing.assert_allclose(arrays[0][2], [0, .05, .1])
        np.testing.assert_allclose(arrays[0][3], [0, 3, 1.5])
        np.testing.assert_array_equal(arrays[1][3], np.zeros(3))

    def test_recent_user_references_persist_without_copying_recordings(self):
        remember_reference(self.path)
        other = self.root/'Another reference.hscope'
        remember_reference(other)
        remember_reference(self.path)
        self.assertEqual(recent_references(), [str(self.path), str(other)])
        self.assertEqual(self.path.read_bytes(), self.source_bytes)
        settings = self.root/'settings/reference-history.json'
        settings.write_text('"/invalid"')
        self.assertEqual(recent_references(), [])

    def window(self):
        self.application = W.QApplication.instance() or W.QApplication([])
        self.application.setQuitOnLastWindowClosed(False)
        window = MainWindow()
        window.timer.stop()
        window.readout_timer.stop()
        window.metadata = dict(self.metadata, channel_order=list(range(1, 9)), zero_offsets_counts=[2000]*8,
                               volts_per_count=[.01]*8, probe_factors_at_record_start=[1]*8)
        raw = np.full((100, 8), 2133, dtype='<u2')
        window.buffer.append(raw)
        window.displayed = (0, raw)
        window.draw(*window.displayed)
        def cleanup():
            window.worker = None
            window.close()
            self.application.processEvents()
        self.addCleanup(cleanup)
        return window

    def settings(self):
        return {'kind': 'recording', 'path': str(self.path), 'vendor_index': 0, 'start_seconds': 0.,
                'duration': .1, 'frame_number': 1, 'shift_ms': 0., 'stretch': 1., 'gain': 1.,
                'vertical_shift': 0., 'shape_duration': 5., 'shape_height': 5., 'opacity': 45,
                'channel_mapping': {str(i+1): i for i in range(8)}}

    def test_overlay_holds_display_without_stopping_recording_or_changing_export(self):
        window = self.window()
        worker = Mock()
        worker.isRunning.return_value = True
        worker.stop_event = threading.Event()
        window.worker = worker
        window.recording_active = window.recording_requested = True
        buffer, metadata, raw = window.buffer, window.metadata, window.displayed[1].copy()
        window.reference_overlay.install(load_recording_reference(self.path), self.settings())
        self.assertTrue(window.held)
        self.assertTrue(window.recording_active and window.recording_requested)
        self.assertIs(window.buffer, buffer)
        self.assertIs(window.metadata, metadata)
        self.assertIsNone(window.reader)
        np.testing.assert_array_equal(window.displayed[1], raw)
        self.assertEqual(window.channel_values[0].text(), '1.330 V')
        worker.stop.assert_not_called()
        worker.stop_recording.assert_not_called()
        csv_path = self.root/'live-only.csv'
        window.export_view(csv_path)
        with csv_path.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 100)
        self.assertEqual(float(rows[0]['CH3_V']), 1.33)
        self.assertFalse(any('REF' in key for key in rows[0]))
        window.reference_overlay.clear()
        self.assertEqual(window.reference_overlay.curves, [])
        self.assertEqual(self.path.read_bytes(), self.source_bytes)

    def test_overlay_follows_mapped_position_and_visibility(self):
        window = self.window()
        overlay = window.reference_overlay
        data = load_recording_reference(self.path)
        overlay.install(data, self.settings())
        window.set_channel_offset(2, 8)
        np.testing.assert_allclose(overlay.curves[1].getData()[1], data.values[:, 1]+8)
        window.channel_checks[2].setChecked(False)
        self.assertFalse(overlay.curves[1].isVisible())
        self.assertTrue(overlay.curves[0].isVisible())
        overlay.visible.setChecked(False)
        self.assertFalse(any(curve.isVisible() for curve in overlay.curves))

    def test_dialog_load_cancel_and_invalid_file_preserve_current_overlay(self):
        window = self.window()
        overlay = window.reference_overlay
        data = load_recording_reference(self.path)
        overlay.install(data, self.settings())
        dialog = ReferenceDialog(overlay)
        dialog.add_file(str(self.root/'Missing.hscope'))
        dialog.apply()
        self.assertIsNone(dialog.result_data)
        self.assertIn('Could not load reference', dialog.error.text())
        dialog.reject()
        self.assertIs(overlay.data, data)
        dialog = ReferenceDialog(overlay)
        dialog.apply()
        self.assertEqual(dialog.result(), W.QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_data.channels, [1, 3])

    def image(self):
        path = self.root/'Waveform screenshot.png'
        image = QtGui.QImage(40, 20, QtGui.QImage.Format.Format_RGBA8888)
        image.fill(QtGui.QColor(0, 0, 0, 0))
        image.setPixelColor(0, 0, QtGui.QColor('red'))
        image.setPixelColor(39, 19, QtGui.QColor('blue'))
        self.assertTrue(image.save(str(path)))
        return path

    def test_image_decoding_preserves_pixels_alpha_and_source(self):
        path = self.image()
        original = path.read_bytes()
        data = load_image_reference(path)
        self.assertEqual(data.kind, 'image')
        self.assertIn('not calibrated', data.note)
        self.assertEqual(data.image.pixelColor(0, 0), QtGui.QColor('red'))
        self.assertEqual(data.image.pixelColor(39, 19), QtGui.QColor('blue'))
        self.assertEqual(data.image.pixelColor(1, 1).alpha(), 0)
        self.assertEqual(path.read_bytes(), original)
        remember_reference(path)
        remember_reference(self.path)
        self.assertIn(str(path), recent_references())

    def test_common_image_formats_and_large_preview_limit(self):
        original = QtGui.QImage(str(self.image()))
        for extension in ('png', 'jpg', 'bmp', 'webp', 'tiff'):
            with self.subTest(extension=extension):
                self.assertIn(extension, image_formats())
                path = self.root/f'example.{extension}'
                self.assertTrue(original.save(str(path)))
                self.assertEqual(load_image_reference(path).image.size(), original.size())
        path = self.root/'reference.svg'
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40"><rect width="100" height="40" fill="red"/></svg>')
        self.assertEqual(load_image_reference(path).image.pixelColor(20, 20), QtGui.QColor('red'))
        with patch('reference_overlay.MAX_IMAGE_EDGE', 10), patch('reference_overlay.MAX_IMAGE_PIXELS', 40):
            data = load_image_reference(self.image())
        self.assertLessEqual(data.image.width(), 10)
        self.assertLessEqual(data.image.width()*data.image.height(), 40)
        self.assertIn('reduced for display', data.note)

    def test_phone_photo_orientation_and_animated_first_frame(self):
        image = QtGui.QImage(40, 20, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('red'))
        path = self.root/'phone.jpg'
        self.assertTrue(image.save(str(path)))
        jpeg = path.read_bytes()
        exif = b'Exif\x00\x00II' + struct.pack('<HIH', 42, 8, 1) + struct.pack('<HHIHHI', 0x112, 3, 1, 6, 0, 0)
        path.write_bytes(jpeg[:2] + b'\xff\xe1' + struct.pack('>H', len(exif)+2) + exif + jpeg[2:])
        rotated = load_image_reference(path).image
        self.assertEqual(rotated.size(), QtCore.QSize(20, 40))
        gif = self.root/'animated.gif'
        gif.write_bytes(base64.b64decode('R0lGODlhAgACAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAAAgACAAAIBgABCAQQEAAh+QQBCgABACwAAAAAAgACAIEAAP8AAAAAAAAAAAAIBgABCAQQEAA7'))
        data = load_image_reference(gif)
        self.assertIn('first frame only', data.note)
        self.assertEqual(data.image.pixelColor(0, 0), QtGui.QColor('red'))

    def test_image_placement_orientation_visibility_and_clear(self):
        window = self.window()
        overlay = window.reference_overlay
        settings = dict(self.settings(), kind='image', path=str(self.image()), image_width=.02,
                        image_height=6., image_bottom=-2., shift_ms=10.)
        overlay.install(load_image_reference(settings['path']), settings)
        item = overlay.image_item
        xleft = window.plot.viewRange()[0][0]+.01
        top_left = item.mapToParent(QtCore.QPointF(0, 0))
        bottom_right = item.mapToParent(QtCore.QPointF(40, 20))
        self.assertAlmostEqual(top_left.x(), xleft)
        self.assertAlmostEqual(top_left.y(), 4)
        self.assertAlmostEqual(bottom_right.x(), xleft+.02)
        self.assertAlmostEqual(bottom_right.y(), -2)
        self.assertAlmostEqual(item.opacity(), .45)
        self.assertLess(item.zValue(), window.curves[0].zValue())
        self.assertIn('IMAGE · visual only', overlay.summary.text())
        window.channel_checks[0].setChecked(False)
        window.set_channel_offset(2, 15)
        self.assertTrue(item.isVisible())
        self.assertEqual(item.mapToParent(QtCore.QPointF(0, 0)), top_left)
        overlay.visible.setChecked(False)
        self.assertFalse(item.isVisible())
        overlay.visible.setChecked(True)
        overlay.fit()
        self.assertLess(window.plot.viewRange()[1][0], -2)
        self.assertGreater(window.plot.viewRange()[1][1], 4)
        overlay.install(load_recording_reference(self.path), self.settings())
        self.assertIsNone(overlay.image_item)
        self.assertIsNone(item.scene())
        self.assertEqual(len(overlay.curves), 2)

    def test_image_dialog_cancel_error_and_saved_settings(self):
        window = self.window()
        overlay = window.reference_overlay
        data = load_recording_reference(self.path)
        overlay.install(data, self.settings())
        dialog = ReferenceDialog(overlay)
        dialog.kind.setCurrentIndex(2)
        dialog.add_file(str(self.root/'Missing.png'))
        dialog.apply()
        self.assertIsNone(dialog.result_data)
        self.assertIs(overlay.data, data)
        self.assertIn('Cannot read this image', dialog.error.text())
        dialog.add_file(str(self.image()))
        dialog.image_width.setValue(.000125)
        dialog.image_bottom.setValue(-7)
        dialog.apply()
        self.assertEqual(dialog.result(), W.QDialog.DialogCode.Accepted)
        self.assertIs(overlay.data, data)
        overlay.install(dialog.result_data, dialog.result_settings)
        restored = ReferenceDialog(overlay)
        self.assertEqual(restored.kind.currentIndex(), 2)
        self.assertEqual(restored.images.currentData(), str(self.root/'Waveform screenshot.png'))
        self.assertAlmostEqual(restored.image_width.value(), .000125)
        self.assertEqual(restored.image_bottom.value(), -7)
        self.assertFalse(restored.form.isRowVisible(restored.start))
        self.assertFalse(restored.form.isRowVisible(restored.gain))
        restored.fit_image()
        self.assertAlmostEqual(restored.image_height.value(), np.ptp(window.plot.viewRange()[1]), places=3)
        restored.reject()
        self.assertEqual(overlay.settings['image_bottom'], -7)

    def test_image_overlay_keeps_live_recording_and_export_unchanged(self):
        window = self.window()
        worker = Mock()
        worker.isRunning.return_value = True
        worker.stop_event = threading.Event()
        window.worker = worker
        window.recording_active = window.recording_requested = True
        raw = window.displayed[1].copy()
        settings = dict(self.settings(), kind='image', image_width=1, image_height=10, image_bottom=-5)
        window.reference_overlay.install(load_image_reference(self.image()), settings)
        self.assertTrue(window.held)
        self.assertTrue(window.recording_active and window.recording_requested)
        worker.stop.assert_not_called()
        worker.stop_recording.assert_not_called()
        np.testing.assert_array_equal(window.displayed[1], raw)
        self.assertEqual(window.channel_values[0].text(), '1.330 V')
        csv_path = self.root/'image-live-only.csv'
        window.export_view(csv_path)
        with csv_path.open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 100)
        self.assertEqual(float(rows[0]['CH1_V']), 1.33)
        self.assertFalse(any('image' in key.lower() for key in rows[0]))
        window.reference_overlay.clear()
        self.assertIsNone(window.reference_overlay.image_item)


if __name__ == '__main__':
    unittest.main()
