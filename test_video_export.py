import errno
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6 import QtGui, QtWidgets as W

from core import CaptureReader, CaptureWriter
from reference_overlay import load_image_reference
from video_export import VideoOptions, VideoPlan, VideoRenderer, VideoWorker, VideoDialog, ExportCancelled, encoder_path, peak_points


class VideoExportTests(unittest.TestCase):
    def setUp(self):
        self.application = W.QApplication.instance() or W.QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'Crank & cam.hscope'
        self.meta = dict(channel_order=[1, 3], channel_names={'1': 'Crank', '3': 'Intake cam'},
                         sample_rate_nominal_hz=1000, zero_offsets_counts=[2000]*2,
                         volts_per_count=[.01]*2, probe_factors_at_record_start=[1, 10], acquisition_mode='roll')
        self.raw = np.column_stack((2000+np.arange(200, dtype='<u2'), np.full(200, 2010, dtype='<u2')))
        writer = CaptureWriter(self.path, self.meta, 300)
        writer.append(300, self.raw, 1)
        writer.close()
        self.original = self.path.read_bytes()
        self.appearance = dict(channels=[1, 3], names={'1': 'Crank', '3': 'Intake cam'},
                               offsets=[2, 0, -2, 0, 0, 0, 0, 0], colors=['#ffc800', '#00ffff', '#ff5577']+['#ffffff']*5,
                               y_range=[-4, 5], probe_factors=[1, 10])
        self.options = VideoOptions(window=.1)

    def reader(self, path=None):
        reader = CaptureReader(path or self.path)
        self.addCleanup(reader.close)
        return reader

    def test_rolling_plan_covers_beginning_end_and_has_no_time_gaps(self):
        reader = self.reader()
        plan = VideoPlan(reader, self.options)
        self.assertEqual(plan.total_frames, 6)
        views = [plan.sample_view(i) for i in range(plan.total_frames)]
        self.assertEqual(views[0][0], reader.start)
        self.assertEqual(views[-1][1], reader.end)
        self.assertAlmostEqual(views[-1][4], .199)
        for before, after in zip(views, views[1:]):
            self.assertLessEqual(after[0], before[1])
        with self.assertRaisesRegex(ValueError, 'covers every part'):
            VideoPlan(reader, VideoOptions(window=.001, speed=1))
        self.assertEqual(VideoPlan(reader, VideoOptions(window=.1, speed=.5)).total_frames, 12)

    def test_burst_export_preserves_every_hardware_frame_and_boundaries(self):
        path = self.root/'frames.hscope'
        writer = CaptureWriter(path, dict(self.meta, acquisition_mode='burst'), 0)
        writer.append(0, self.raw[:20], 1)
        writer.append(20, self.raw[20:55], 9000000000)
        writer.close()
        plan = VideoPlan(self.reader(path), VideoOptions(seconds_per_frame=.1))
        self.assertEqual(plan.total_frames, 6)
        views = [plan.sample_view(i) for i in range(6)]
        self.assertEqual([view[-1] for view in views], [0, 0, 0, 1, 1, 1])
        self.assertEqual(views[0][:2], (0, 20))
        self.assertEqual(views[-1][:2], (20, 55))
        self.assertTrue(all(view[4] is None for view in views))

    def test_peak_reduction_retains_short_pulses_and_order(self):
        x = np.arange(10000)
        y = np.zeros(10000)
        y[1401], y[9998] = 8, -4
        rx, ry = peak_points(x, y, 100)
        self.assertEqual(float(np.max(ry)), 8)
        self.assertEqual(float(np.min(ry)), -4)
        self.assertEqual(rx[0], 0)
        self.assertEqual(rx[-1], 9999)
        self.assertTrue(np.all(np.diff(rx) > 0))

    def test_image_overlay_renders_without_modifying_measurements_or_file(self):
        path = self.root/'reference.png'
        image = QtGui.QImage(20, 10, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('red'))
        image.save(str(path))
        data = load_image_reference(path)
        reader = self.reader()
        plain = VideoRenderer(reader, self.options, self.appearance).render(0)
        appearance = dict(self.appearance, reference=data, reference_settings=dict(opacity=50, shift_ms=0,
                          image_width=.1, image_height=9, image_bottom=-4))
        overlaid = VideoRenderer(reader, self.options, appearance).render(0)
        self.assertGreater(overlaid.pixelColor(91, 156).red(), plain.pixelColor(91, 156).red())
        np.testing.assert_array_equal(reader.window(300, 500)[1], self.raw)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_bundled_encoder_writes_decodable_complete_mp4(self):
        destination = self.root/'complete.mp4'
        worker = VideoWorker(self.path, destination, self.options, self.appearance)
        self.assertEqual(worker.encode(), str(destination))
        result = subprocess.run([encoder_path(), '-v', 'error', '-i', str(destination), '-f', 'framemd5', '-'],
                                capture_output=True, text=True, timeout=30, check=True)
        frames = [line for line in result.stdout.splitlines() if line and not line.startswith('#')]
        self.assertEqual(len(frames), 6)
        self.assertIn('#dimensions 0: 1280x720', result.stdout)
        self.assertNotEqual(frames[0].rsplit(',', 1)[1], frames[-1].rsplit(',', 1)[1])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('.traceforge-video-*')), [])
        with self.assertRaises(FileExistsError):
            VideoWorker(self.path, destination, self.options, self.appearance).encode()

    def test_cancel_and_encoder_failure_remove_partial_files(self):
        destination = self.root/'cancelled.mp4'
        worker = VideoWorker(self.path, destination, self.options, self.appearance)
        worker.progress.connect(lambda *_: worker.cancel())
        with self.assertRaises(ExportCancelled):
            worker.encode()
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.traceforge-video-*')), [])
        with patch('video_export.encoder_path', return_value='/missing/encoder'), self.assertRaises(FileNotFoundError):
            VideoWorker(self.path, destination, self.options, self.appearance).encode()
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.traceforge-video-*')), [])

    def test_video_can_save_on_filesystem_without_hard_links(self):
        destination = self.root/'external-drive.mp4'
        with patch('video_export.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'No hard links')):
            VideoWorker(self.path, destination, self.options, self.appearance).encode()
        self.assertGreater(destination.stat().st_size, 1000)
        self.assertEqual(list(self.root.glob('.traceforge-video-*')), [])

    def test_export_dialog_background_progress_and_cancel(self):
        from app import MainWindow
        window = MainWindow()
        window.timer.stop()
        window.readout_timer.stop()
        window.open_capture(self.path)
        dialog = VideoDialog(window)
        dialog.speed.setValue(.01)
        destination = self.root/'dialog-cancel.mp4'
        try:
            with patch.object(W.QFileDialog, 'getSaveFileName', return_value=(str(destination), 'MP4')):
                dialog.start()
            self.assertTrue(dialog.worker.isRunning())
            self.assertFalse(dialog.save_button.isEnabled())
            dialog.reject()
            deadline = time.monotonic()+10
            while dialog.worker.isRunning() and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(.01)
            self.application.processEvents()
            self.assertFalse(dialog.worker.isRunning())
            self.assertIn('cancelled', dialog.status.text().lower())
            self.assertFalse(destination.exists())
            self.assertEqual(self.path.read_bytes(), self.original)
        finally:
            if dialog.worker is not None:
                dialog.worker.cancel()
                dialog.worker.wait(10000)
            dialog.reject()
            window.close()


if __name__ == '__main__':
    unittest.main()
