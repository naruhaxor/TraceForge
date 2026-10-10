from dataclasses import dataclass
import errno
import importlib
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W
from pyqtgraph.functions import arrayToQPath

from core import CaptureReader, format_time, to_volts
from reference_overlay import transformed_reference


def encoder_path():
    if getattr(sys, 'frozen', False):
        path = Path(sys._MEIPASS)/'native/ffmpeg'
        if not path.is_file():
            raise RuntimeError('The bundled video encoder is missing.')
        return str(path)
    return importlib.import_module('imageio_ffmpeg').get_ffmpeg_exe()


@dataclass
class VideoOptions:
    width: int = 1280
    height: int = 720
    fps: int = 30
    speed: float = 1.
    window: float = 5.
    seconds_per_frame: float = .5


class VideoPlan:
    def __init__(self, reader, options):
        self.reader, self.options = reader, options
        self.rate = float(reader.metadata['sample_rate_nominal_hz'])
        self.duration = reader.count/self.rate
        if not reader.count:
            raise ValueError('This recording has no samples to export.')
        if options.fps not in (30, 60) or (options.width, options.height) not in ((1280, 720), (1920, 1080)):
            raise ValueError('Choose 720p or 1080p at 30 or 60 fps.')
        if not all(math.isfinite(v) and v > 0 for v in (options.speed, options.window, options.seconds_per_frame)):
            raise ValueError('Video speed and time settings must be positive.')
        if reader.mode == 'burst':
            self.hold_frames = max(1, round(options.seconds_per_frame*options.fps))
            self.total_frames = len(reader.frames)*self.hold_frames
        else:
            if options.window < min(self.duration, options.speed/options.fps):
                raise ValueError('Increase the visible time window or reduce playback speed so the video covers every part of the recording.')
            if min(options.window, self.duration)*self.rate > 5000000:
                raise ValueError('Choose a smaller visible time window (at most 5 million samples per video frame).')
            self.total_frames = max(1, math.ceil(self.duration/options.speed*options.fps))

    def sample_view(self, video_frame):
        if not 0 <= video_frame < self.total_frames:
            raise IndexError('Video frame outside the recording.')
        if self.reader.mode == 'burst':
            frame = video_frame//self.hold_frames
            start, count = self.reader.frames[frame]
            return start, start+count, 0., count/self.rate, None, frame
        cursor = min(video_frame*self.options.speed/self.options.fps, (self.reader.count-1)/self.rate)
        if video_frame == self.total_frames-1:
            cursor = (self.reader.count-1)/self.rate
        width = min(self.options.window, self.duration)
        left = max(0., min(cursor-width*.8, self.duration-width))
        start = self.reader.start+int(left*self.rate)
        stop = min(self.reader.end, start+max(1, math.ceil(width*self.rate)))
        if video_frame == self.total_frames-1:
            stop = self.reader.end
        return start, stop, left, left+width, cursor, None


def peak_points(x, y, pixels):
    if len(x) <= pixels*2:
        return x, y
    step = math.ceil(len(x)/pixels)
    full = len(x)//step*step
    blocks = y[:full].reshape(-1, step)
    base = np.arange(len(blocks))*step
    indexes = np.sort(np.column_stack((base+np.argmin(blocks, axis=1), base+np.argmax(blocks, axis=1))).ravel())
    if full < len(x):
        tail = y[full:]
        indexes = np.r_[indexes, np.sort([full+np.argmin(tail), full+np.argmax(tail)]), len(x)-1]
    indexes = np.unique(np.r_[0, indexes, len(x)-1])
    return x[indexes], y[indexes]


class VideoRenderer:
    def __init__(self, reader, options, appearance):
        self.reader, self.options, self.appearance = reader, options, appearance
        self.plan = VideoPlan(reader, options)

    def render(self, index):
        o, a = self.options, self.appearance
        start, stop, left, right, cursor, frame = self.plan.sample_view(index)
        _, raw = self.reader.window(start, stop)
        volts = to_volts(raw, self.reader.metadata, a['probe_factors'])
        if not np.all(np.isfinite(volts)):
            raise ValueError('Recording contains invalid voltage conversion data.')
        x = (np.arange(start, stop)-(start if frame is not None else self.reader.start))/self.plan.rate
        image = QtGui.QImage(o.width, o.height, QtGui.QImage.Format.Format_RGB888)
        image.fill(QtGui.QColor('#14191f'))
        p = QtGui.QPainter(image)
        try:
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.scale(o.width/1280, o.height/720)
            rect = QtCore.QRectF(88, 152, 902, 450)
            low, high = a['y_range']
            if not np.isfinite([low, high]).all() or high <= low:
                raise ValueError('The graph voltage range is invalid.')
            def text(x, y, value, size=12, color='#c4d2dd'):
                p.setFont(QtGui.QFont('Sans Serif', size))
                p.setPen(QtGui.QColor(color))
                p.drawText(QtCore.QPointF(x, y), value)
            def px(value):
                return rect.left()+(value-left)/(right-left)*rect.width()
            def py(value):
                return rect.bottom()-(value-low)/(high-low)*rect.height()
            def trace(tx, ty, color, dashed=False):
                tx, ty = peak_points(np.asarray(tx), np.asarray(ty), round(rect.width()*o.width/1280))
                if not len(tx):
                    return
                pen = QtGui.QPen(QtGui.QColor(color), 1.4)
                if dashed:
                    pen.setStyle(QtCore.Qt.PenStyle.DashLine)
                p.setPen(pen)
                if len(tx) == 1:
                    p.drawEllipse(QtCore.QPointF(px(tx[0]), py(ty[0])), 2, 2)
                else:
                    p.drawPath(arrayToQPath(px(tx), py(ty), finiteCheck=True))
            text(30, 38, 'TraceForge', 23, '#eef5fb')
            text(222, 38, 'for Linux · recording video', 12)
            p.setFont(QtGui.QFont('Sans Serif', 13))
            filename = p.fontMetrics().elidedText(self.reader.path.name, QtCore.Qt.TextElideMode.ElideMiddle, 1200)
            text(30, 68, filename, 13)
            if frame is None:
                text(30, 99, f'Recording time: {format_time(cursor)} / {format_time(self.plan.duration)} · {o.speed:g}× playback · nominal timing', 13)
            else:
                text(30, 99, f'Capture frame {frame+1:,} / {len(self.reader.frames):,} · independent frames · gaps unknown', 13)
            ref = a.get('reference')
            if ref:
                kind = 'IMAGE · visual only' if ref.kind == 'image' else 'SHAPE ONLY · unverified units/time' if ref.kind == 'vendor' else 'REFERENCE · display alignment applied'
                p.setFont(QtGui.QFont('Sans Serif', 10))
                note = p.fontMetrics().elidedText(f'{kind}: {ref.title}', QtCore.Qt.TextElideMode.ElideMiddle, 1220)
                text(30, 125, note, 10, '#9eafbd')
            p.fillRect(rect, QtGui.QColor('#101820'))
            p.save()
            p.setClipRect(rect)
            if ref:
                settings = a['reference_settings']
                p.setOpacity(settings['opacity']/100)
                if ref.kind == 'image':
                    xl = left+settings['shift_ms']/1000
                    bottom = settings['image_bottom']
                    destination = QtCore.QRectF(px(xl), py(bottom+settings['image_height']),
                                               settings['image_width']/(right-left)*rect.width(), settings['image_height']/(high-low)*rect.height())
                    p.drawImage(destination, ref.image)
                else:
                    mapping = [settings['channel_mapping'].get(str(ch)) for ch in ref.channels]
                    params = {key: settings[key] for key in ('shift_ms', 'stretch', 'gain', 'vertical_shift', 'shape_duration', 'shape_height')}
                    for _, target, tx, ty in transformed_reference(ref, mapping, a['offsets'], left, **params):
                        if target+1 in a['channels']:
                            trace(tx, ty, a['colors'][target], True)
                p.setOpacity(1.)
            p.setPen(QtGui.QPen(QtGui.QColor('#304350'), .7))
            for i in range(11):
                gx = rect.left()+i*rect.width()/10
                p.drawLine(QtCore.QPointF(gx, rect.top()), QtCore.QPointF(gx, rect.bottom()))
            for i in range(9):
                gy = rect.top()+i*rect.height()/8
                p.drawLine(QtCore.QPointF(rect.left(), gy), QtCore.QPointF(rect.right(), gy))
            for column, ch in enumerate(self.reader.channel_order):
                if ch in a['channels']:
                    trace(x, volts[:, column]+a['offsets'][ch-1], a['colors'][ch-1])
            if cursor is not None:
                p.setPen(QtGui.QPen(QtGui.QColor('#ffffff'), 1., QtCore.Qt.PenStyle.DashLine))
                p.drawLine(QtCore.QPointF(px(cursor), rect.top()), QtCore.QPointF(px(cursor), rect.bottom()))
            p.restore()
            for i in range(6):
                value = left+i*(right-left)/5
                text(rect.left()+i*rect.width()/5-18, rect.bottom()+24, format_time(value), 10)
            for i in range(9):
                value = high-i*(high-low)/8
                text(20, rect.top()+i*rect.height()/8+4, f'{value:.4g}', 10)
            text(30, 143, 'V + offset', 10)
            text(300, 650, 'Time within captured frame · nominal' if frame is not None else 'Time from recording start · nominal', 12)
            readout_row = len(raw)-1 if cursor is None else min(len(raw)-1, max(0, round((cursor-left)*self.plan.rate)))
            for slot, ch in enumerate(a['channels']):
                y = rect.top()+slot*rect.height()/len(a['channels'])
                name = a['names'].get(str(ch), '')
                title = f'CH{ch} - {name}' if name else f'CH{ch}'
                p.setFont(QtGui.QFont('Sans Serif', 11))
                title = p.fontMetrics().elidedText(title, QtCore.Qt.TextElideMode.ElideRight, 236)
                text(1010, y+17, title, 11, a['colors'][ch-1])
                column = self.reader.channel_order.index(ch)
                text(1010, y+40, f'{volts[readout_row, column]:.3f} V', 17, a['colors'][ch-1])
            warning = 'Incomplete source: committed samples only · ' if not self.reader.metadata.get('complete') else ''
            text(30, 686, warning+'Readouts show measured voltage; trace positions include display offsets. Video is a visual export.', 10, '#9eafbd')
        finally:
            p.end()
        return image


class ExportCancelled(Exception):
    pass


class VideoWorker(QtCore.QThread):
    progress = QtCore.Signal(int, int)
    succeeded = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()

    def __init__(self, source, destination, options, appearance, parent=None):
        super().__init__(parent)
        self.source, self.destination = Path(source), Path(destination)
        self.options, self.appearance = options, appearance
        self.stop_event = threading.Event()
        self.process = None

    def cancel(self):
        self.stop_event.set()
        process = self.process
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass

    def encode(self):
        if self.destination.exists() or self.destination.is_symlink():
            raise FileExistsError('Choose a new video filename; the destination already exists.')
        reader = CaptureReader(self.source)
        temporary = None
        try:
            renderer = VideoRenderer(reader, self.options, self.appearance)
            if not self.appearance['channels']:
                raise ValueError('Show at least one recorded channel before exporting video.')
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.traceforge-video-', suffix='.mp4', dir=self.destination.parent)
            os.close(fd)
            temporary = Path(name)
            o = self.options
            command = [encoder_path(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                       '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{o.width}x{o.height}',
                       '-framerate', str(o.fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'veryfast',
                       '-crf', '20', '-pix_fmt', 'yuv420p', '-threads', '2', '-movflags', '+faststart', str(temporary)]
            with tempfile.TemporaryFile() as errors:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
                try:
                    for index in range(renderer.plan.total_frames):
                        if self.stop_event.is_set():
                            raise ExportCancelled()
                        image = renderer.render(index)
                        self.process.stdin.write(image.constBits())
                        self.progress.emit(index+1, renderer.plan.total_frames)
                    self.process.stdin.close()
                    code = self.process.wait()
                    if self.stop_event.is_set():
                        raise ExportCancelled()
                    if code:
                        errors.seek(0)
                        raise RuntimeError(errors.read(3000).decode('utf-8', 'replace') or f'Video encoder exited with code {code}.')
                except BrokenPipeError:
                    if self.stop_event.is_set():
                        raise ExportCancelled() from None
                    errors.seek(0)
                    raise RuntimeError(errors.read(3000).decode('utf-8', 'replace') or 'Video encoder stopped unexpectedly.') from None
            if self.stop_event.is_set():
                raise ExportCancelled()
            try:
                os.link(temporary, self.destination)
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV):
                    raise
                created = False
                try:
                    with self.destination.open('xb') as output, temporary.open('rb') as source:
                        created = True
                        while block := source.read(1024*1024):
                            if self.stop_event.is_set():
                                raise ExportCancelled()
                            output.write(block)
                except Exception:
                    if created:
                        self.destination.unlink(missing_ok=True)
                    raise
            return str(self.destination)
        finally:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                if self.process.stdin and not self.process.stdin.closed:
                    try:
                        self.process.stdin.close()
                    except OSError:
                        pass
                self.process = None
            reader.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def run(self):
        try:
            self.succeeded.emit(self.encode())
        except ExportCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class VideoDialog(W.QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.worker = None
        self.setWindowTitle('Export recording as video · TraceForge')
        self.setMinimumWidth(640)
        layout = W.QVBoxLayout(self)
        info = W.QLabel('Export the entire recording as an MP4, using the visible channels, names, probe multipliers and vertical positions. The source recording stays unchanged.')
        info.setWordWrap(True)
        layout.addWidget(info)
        form = W.QFormLayout()
        self.resolution = W.QComboBox()
        self.resolution.addItem('720p · 1280 × 720', (1280, 720))
        self.resolution.addItem('1080p · 1920 × 1080', (1920, 1080))
        form.addRow('Resolution', self.resolution)
        self.fps = W.QComboBox()
        for fps in (30, 60):
            self.fps.addItem(f'{fps} fps', fps)
        form.addRow('Video frame rate', self.fps)
        self.speed = W.QDoubleSpinBox()
        self.speed.setDecimals(3)
        self.speed.setRange(.001, 1000)
        self.speed.setValue(1)
        self.speed.setSuffix(' ×')
        form.addRow('Playback speed', self.speed)
        self.span = W.QDoubleSpinBox()
        self.span.setDecimals(6)
        self.span.setRange(.000001, 600)
        self.span.setValue(window.time_window.currentData())
        self.span.setSuffix(' s')
        form.addRow('Visible time window', self.span)
        self.hold = W.QDoubleSpinBox()
        self.hold.setRange(.05, 60)
        self.hold.setValue(.5)
        self.hold.setSuffix(' s')
        form.addRow('Show each captured frame for', self.hold)
        burst = window.reader.mode == 'burst'
        form.setRowVisible(self.speed, not burst)
        form.setRowVisible(self.span, not burst)
        form.setRowVisible(self.hold, burst)
        self.overlay = W.QCheckBox('Include the current reference overlay')
        present = window.reference_overlay.data is not None and window.reference_overlay.visible.isChecked()
        self.overlay.setEnabled(present)
        self.overlay.setChecked(present)
        form.addRow('', self.overlay)
        layout.addLayout(form)
        self.estimate = W.QLabel()
        self.estimate.setWordWrap(True)
        layout.addWidget(self.estimate)
        self.progress = W.QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = W.QLabel()
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Save | W.QDialogButtonBox.StandardButton.Cancel)
        self.save_button = controls.button(W.QDialogButtonBox.StandardButton.Save)
        self.save_button.setText('Export MP4…')
        self.cancel_button = controls.button(W.QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(self.start)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.inputs = [self.resolution, self.fps, self.speed, self.span, self.hold, self.overlay]
        for control in (self.speed, self.span, self.hold):
            control.setKeyboardTracking(False)
            control.valueChanged.connect(self.update_estimate)
        self.fps.currentIndexChanged.connect(self.update_estimate)
        self.update_estimate()

    def options(self):
        width, height = self.resolution.currentData()
        return VideoOptions(width, height, self.fps.currentData(), self.speed.value(), self.span.value(), self.hold.value())

    def update_estimate(self, *_):
        try:
            plan = VideoPlan(self.window.reader, self.options())
            self.estimate.setText(f'Entire recording · {plan.total_frames:,} video frames · video length {format_time(plan.total_frames/self.options().fps)}' +
                                  ('\nEach hardware frame stays separate. Time between captures is unknown.' if self.window.reader.mode == 'burst' else '\nThe moving cursor marks recording time. Video speed does not change the recorded time scale.'))
            self.save_button.setEnabled(True)
        except ValueError as exc:
            self.estimate.setText(str(exc))
            self.save_button.setEnabled(False)

    def start(self):
        from app import COLORS
        window = self.window
        channels = [ch for ch in window.recorded_channels() if window.channel_checks[ch-1].isChecked()]
        if not channels:
            self.status.setText('Show at least one channel before exporting video.')
            return
        path, _ = W.QFileDialog.getSaveFileName(self, 'Save recording video', str(window.reader.path.with_suffix('.mp4')), 'MP4 video (*.mp4)',
                                               options=W.QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        if not path.lower().endswith('.mp4'):
            path += '.mp4'
        if Path(path).exists() or Path(path).is_symlink():
            self.status.setText('That filename already exists. Choose a new name to keep the earlier file.')
            return
        appearance = dict(channels=channels, names=dict(window.channel_names), offsets=window.channel_offsets.copy(),
                          colors=list(COLORS), y_range=list(window.plot.viewRange()[1]), probe_factors=list(window.probe_factors()))
        if self.overlay.isChecked():
            appearance.update(reference=window.reference_overlay.data, reference_settings=dict(window.reference_overlay.settings))
        self.worker = VideoWorker(window.reader.path, path, self.options(), appearance, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.succeeded.connect(lambda result: self.status.setText(f'Video saved: {result}'))
        self.worker.failed.connect(lambda error: self.status.setText(f'Video export failed: {error}'))
        self.worker.cancelled.connect(lambda: self.status.setText('Export cancelled. The partial video was removed.'))
        self.worker.finished.connect(self.finished_export)
        for control in self.inputs:
            control.setEnabled(False)
        self.save_button.setEnabled(False)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText('Rendering the complete recording…')
        self.cancel_button.setText('Cancel')
        self.worker.start()

    def on_progress(self, current, total):
        self.progress.setValue(round(1000*current/total))
        self.status.setText(f'Encoding frame {current:,} of {total:,}' if current < total else 'Finalizing MP4…')

    def finished_export(self):
        for control in self.inputs:
            control.setEnabled(True)
        self.overlay.setEnabled(self.window.reference_overlay.data is not None and self.window.reference_overlay.visible.isChecked())
        self.cancel_button.setText('Close')
        self.cancel_button.setEnabled(True)
        self.update_estimate()

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText('Cancelling export…')
            self.cancel_button.setEnabled(False)
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.reject()
            event.ignore()
        else:
            event.accept()
