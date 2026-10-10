"""Explicit package diagnostics; hardware capture is opt-in via --test-usb."""
import json
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import time
import subprocess

import numpy as np
import usb.core
import usb.backend.libusb1
from PySide6 import QtCore, QtGui, QtWidgets as W

from core import CaptureReader, CaptureWriter
from paths import recordings_directory
from device_detection import detect_device
from reference_overlay import load_recording_reference, load_vendor_reference, load_image_reference, image_formats
from video_export import VideoWorker, VideoOptions, encoder_path


def verify_package(application, window, report_path, test_usb=False):
    capture_dir = recordings_directory()
    report = {"frozen": bool(getattr(sys, "frozen", False)), "python": sys.version,
              "captures_directory": str(capture_dir), "hardware_capture_requested": test_usb}
    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda kind, value, tb: errors.append(f"{kind.__name__}: {value}")

    def wait_for(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            application.processEvents()
            if window.last_error or errors:
                raise RuntimeError(window.last_error or str(errors))
            if time.monotonic() > deadline:
                raise TimeoutError("Package diagnostic timed out")
            time.sleep(.01)
        application.processEvents()

    try:
        application.processEvents()
        backend = usb.backend.libusb1.get_backend()
        assert backend is not None, "Bundled libusb backend did not load"
        report["usb_backend"] = str(backend.lib._name)
        detection = detect_device()
        report['device_detection'] = asdict(detection)
        window.apply_detection(detection)
        report["scope_detected"] = detection.count > 0
        report["reference_examples"] = len(window.library.entries)
        assert report["reference_examples"] == 86
        assert not window.windowIcon().pixmap(64, 64).isNull(), "Application icon did not render"
        assert window.readout_timer.interval() == 20
        report["numeric_refresh_ms"] = window.readout_timer.interval()
        assert window.time_window.count() == 25
        report["time_scales"] = window.time_window.count()
        def name_recording():
            dialog = application.activeModalWidget()
            dialog.findChild(W.QLineEdit, 'recordingName').setText('Package naming check')
            dialog.findChild(W.QDialogButtonBox).button(W.QDialogButtonBox.StandardButton.Save).click()
        QtCore.QTimer.singleShot(0, name_recording)
        named_path = window.prompt_recording_path()
        assert named_path.name == 'Package naming check.hscope'
        assert named_path.parent.parent == capture_dir and not named_path.exists()
        report['named_recording_prompt'] = True
        capture_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="package-check-", dir=capture_dir) as directory:
            path = Path(directory) / "recording.hscope"
            meta = {"channel_order": [1, 3], "zero_offsets_counts": [2000, 2000],
                    "volts_per_count": [.01, .01], "sample_rate_nominal_hz": 1000,
                    "acquisition_mode": "roll", "probe_factors_at_record_start": [1, 1],
                    "channel_names": {"1": "Crank", "3": "Intake cam"}}
            values = np.tile([2133, 1841], (5000, 1)).astype("<u2")
            writer = CaptureWriter(path, meta, 0)
            writer.append(0, values, time.monotonic_ns())
            writer.close()
            window.open_capture(path)
            assert window.last_error is None, window.last_error
            assert window.channel_values[0].text() == "1.330 V"
            assert window.channel_values[2].text() == "-1.590 V"
            assert window.name_controls[0].text() == 'Crank'
            assert window.curves[2].name() == 'Intake cam'
            assert window.channel_baselines[0].label.textItem.toPlainText() == 'Crank · 1.330 V'
            assert window.channel_baselines[2].label.textItem.toPlainText() == 'Intake cam · -1.590 V'
            report['graph_voltage_readouts'] = True
            for mode, value in [('LOW', '1.33 V'), ('HIGH', '1.3 V'), ('OFF', '1.330 V')]:
                window.noise_filter_button.click()
                assert window.noise_filter_button.text() == f'Noise filter: {mode}'
                assert window.channel_values[0].text() == value
                assert window.channel_baselines[0].label.textItem.toPlainText() == 'Crank · ' + value
            report['noise_filter_readout_precision'] = True
            assert window.video_action.isEnabled()
            report['recorded_channel_names'] = True
            window.position_controls[0].setValue(10)
            window.channel_baselines[2].setValue(-4)
            assert np.isclose(window.curves[0].getData()[1][-1], 11.33)
            assert np.isclose(window.curves[2].getData()[1][-1], -5.59)
            assert window.channel_values[0].text() == "1.330 V"
            assert window.channel_values[2].text() == "-1.590 V"
            assert window.channel_baselines[0].label.textItem.toPlainText() == 'Crank · 1.330 V'
            assert window.channel_baselines[2].label.textItem.toPlainText() == 'Intake cam · -1.590 V'
            report['independent_trace_positions'] = True
            original_recording = path.read_bytes()
            window.noise_filter_button.click()
            window.noise_filter_button.click()
            window.graph_filter_button.click()
            assert np.isclose(window.curves[0].getOriginalDataset()[1][-1], 11.3)
            assert np.isclose(window.curves[2].getOriginalDataset()[1][-1], -5.6)
            assert path.read_bytes() == original_recording
            np.testing.assert_array_equal(window.view_samples[1], values)
            window.graph_filter_button.click()
            window.noise_filter_button.click()
            assert np.isclose(window.curves[0].getOriginalDataset()[1][-1], 11.33)
            report['graph_filter_is_display_only'] = True
            window.export_view(Path(directory) / "view.csv")
            assert (Path(directory) / "view.csv.metadata.json").exists()
            settings = {'kind': 'recording', 'path': str(path), 'vendor_index': 0, 'start_seconds': 0.,
                        'duration': 1., 'frame_number': 1, 'shift_ms': 0., 'stretch': 1., 'gain': 1.,
                        'vertical_shift': 0., 'shape_duration': 5., 'shape_height': 5., 'opacity': 45,
                        'channel_mapping': {str(i+1): i for i in range(8)}}
            window.reference_overlay.install(load_recording_reference(path, duration=1), settings)
            assert len(window.reference_overlay.curves) == 2
            assert np.isclose(window.reference_overlay.curves[0].getData()[1][-1], 11.33)
            assert window.channel_values[0].text() == '1.330 V'
            entry = window.library.entries[0]
            vendor = load_vendor_reference(entry, window.library.arrays, 'Bundled example')
            window.reference_overlay.install(vendor, dict(settings, kind='vendor'))
            assert 'SHAPE ONLY' in window.reference_overlay.summary.text()
            window.reference_overlay.clear()
            report['user_and_vendor_reference_overlays'] = True
            image = QtGui.QImage(80, 40, QtGui.QImage.Format.Format_RGBA8888)
            image.fill(QtGui.QColor(20, 100, 230, 128))
            for extension in ('png', 'jpg', 'bmp', 'webp', 'tiff'):
                image_path = Path(directory)/f'reference.{extension}'
                assert image.save(str(image_path)), f'{extension} image writer missing'
                loaded = load_image_reference(image_path)
                assert loaded.image.size() == image.size(), f'{extension} image reader failed'
                if extension == 'png':
                    assert loaded.image.pixelColor(0, 0).alpha() == 128
                    window.reference_overlay.install(loaded, dict(settings, kind='image', path=str(image_path),
                                                                 image_width=2., image_height=4., image_bottom=-2.))
                    item = window.reference_overlay.image_item
                    assert np.isclose(item.mapToParent(QtCore.QPointF(0, 0)).y(), 2.)
                    assert np.isclose(item.mapToParent(QtCore.QPointF(80, 40)).y(), -2.)
                    assert 'IMAGE · visual only' in window.reference_overlay.summary.text()
                    assert window.channel_values[0].text() == '1.330 V'
            svg_path = Path(directory)/'reference.svg'
            svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="red"/></svg>')
            assert load_image_reference(svg_path).image.pixelColor(20, 20) == QtGui.QColor('red')
            assert not window.grab().isNull(), 'Image overlay did not render'
            window.reference_overlay.clear()
            assert window.reference_overlay.image_item is None
            report['image_reference_overlays'] = True
            report['image_formats'] = image_formats()
            destination = Path(directory)/'recording.mp4'
            appearance = dict(channels=[1, 3], names=dict(window.channel_names), offsets=list(window.channel_offsets),
                              colors=['#ffc800', '#00ffff', '#ff5577']+['#ffffff']*5,
                              y_range=list(window.plot.viewRange()[1]), probe_factors=window.probe_factors())
            video = VideoWorker(path, destination, VideoOptions(speed=100, window=5), appearance)
            video.encode()
            decoded = subprocess.run([encoder_path(), '-v', 'error', '-i', str(destination), '-f', 'framemd5', '-'],
                                     capture_output=True, text=True, timeout=30, check=True)
            frames = [line for line in decoded.stdout.splitlines() if line and not line.startswith('#')]
            assert len(frames) == 2 and destination.stat().st_size > 1000
            report['mp4_export_and_decode'] = True
            report['bundled_video_encoder'] = encoder_path()
            window.reader.close()
            window.reader = None
        report["record_replay_export"] = True
        report["preview_rendered"] = not window.grab().isNull()
        if test_usb:
            assert detection.ready, detection.title + ': ' + detection.detail
            window.select_channels([0])
            window.time_window.setCurrentIndex(window.time_window.findData(5.0))
            window.start_acquisition()
            wait_for(lambda: window.metadata is not None and window.buffer.total > 100)
            def name_hardware_recording():
                dialog = application.activeModalWidget()
                dialog.findChild(W.QLineEdit, 'recordingName').setText('Hardware check')
                dialog.findChild(W.QDialogButtonBox).button(W.QDialogButtonBox.StandardButton.Save).click()
            QtCore.QTimer.singleShot(0, name_hardware_recording)
            window.toggle_record()
            wait_for(lambda: window.recording_active)
            start = window.buffer.total
            wait_for(lambda: window.buffer.total >= start + 1000)
            report["live_readout"] = window.channel_values[0].text()
            window.stop_acquisition()
            wait_for(lambda: window.worker is None)
            reader = CaptureReader(window.last_recording)
            assert reader.metadata["complete"] and reader.count > 0
            assert window.last_recording.name == 'Hardware check.hscope'
            assert window.last_recording.parent.parent == capture_dir
            report['named_recording_date_folder'] = True
            report["hardware_recording"] = {"path": str(window.last_recording), "rows": reader.count,
                                             "channel_order": reader.channel_order, "complete": True}
            reader.close()
        report["passed"] = not errors
        report["errors"] = errors
    except Exception as exc:
        report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if window.active():
            window.stop_acquisition()
            try:
                wait_for(lambda: window.worker is None)
            except Exception as exc:
                report.update(passed=False, shutdown_error=str(exc))
        window.close()
        application.processEvents()
        sys.excepthook = old_hook
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report.get("passed") else 1
