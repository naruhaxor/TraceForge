from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from core import CaptureReader, to_volts
from paths import data_directory, recordings_directory
from channel_names import normalize_names

MAX_REFERENCE_ROWS = 300000
MAX_IMAGE_EDGE = 4096
MAX_IMAGE_PIXELS = 8000000


@dataclass
class ReferenceData:
    title: str
    x: np.ndarray
    values: np.ndarray
    channels: list
    kind: str
    note: str


@dataclass
class ImageReferenceData:
    title: str
    image: QtGui.QImage
    note: str
    kind: str = 'image'


def image_formats():
    return sorted({bytes(value).decode('ascii') for value in QtGui.QImageReader.supportedImageFormats()})


def load_image_reference(path):
    reader = QtGui.QImageReader(str(path))
    reader.setAutoTransform(True)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if size.isValid():
        scale = min(1., MAX_IMAGE_EDGE/max(size.width(), size.height()),
                    (MAX_IMAGE_PIXELS/(size.width()*size.height()))**.5)
        if scale < 1:
            reader.setScaledSize(QtCore.QSize(max(1, int(size.width()*scale)), max(1, int(size.height()*scale))))
    animated = reader.supportsAnimation()
    image = reader.read()
    if image.isNull():
        raise ValueError(f'Cannot read this image: {reader.errorString()}. Supported formats: {", ".join(image_formats())}.')
    scale = min(1., MAX_IMAGE_EDGE/max(image.width(), image.height()),
                (MAX_IMAGE_PIXELS/(image.width()*image.height()))**.5)
    if scale < 1:
        image = image.scaled(max(1, int(image.width()*scale)), max(1, int(image.height()*scale)),
                             QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
    note = f'IMAGE · visual comparison only; pixels are not calibrated volts or time · {image.width()} × {image.height()} pixels'
    if animated:
        note += ' · first frame only'
    if size.isValid() and max(size.width(), size.height()) > max(image.width(), image.height()):
        note += ' · reduced for display; original file unchanged'
    return ImageReferenceData(Path(path).name, image, note)


def load_recording_reference(path, start_seconds=0., duration=5., frame=0):
    if not np.isfinite(start_seconds) or start_seconds < 0 or not np.isfinite(duration) or duration <= 0:
        raise ValueError('Choose a nonnegative start time and a positive length.')
    reader = CaptureReader(path)
    try:
        if not reader.count:
            raise ValueError('This recording has no committed samples.')
        rate = float(reader.metadata['sample_rate_nominal_hz'])
        if reader.mode == 'burst':
            if frame not in range(len(reader.frames)):
                raise ValueError(f'Choose a frame from 1 to {len(reader.frames)}.')
            start, count = reader.frames[frame]
            stop = start + min(count, MAX_REFERENCE_ROWS)
            segment = f'Frame {frame+1}/{len(reader.frames)} · independent frame'
        else:
            start = reader.start + round(start_seconds * rate)
            if start >= reader.end:
                raise ValueError('The reference start is past the end of this recording.')
            count = min(reader.end-start, max(1, round(duration*rate)))
            stop = start + min(count, MAX_REFERENCE_ROWS)
            segment = f'Start {start_seconds:g} s · loaded {(stop-start)/rate:g} s'
        _, raw = reader.window(start, stop)
        factors = np.asarray(reader.metadata.get('probe_factors_at_record_start', [1]*reader.channel_count), dtype=float)
        if factors.shape != (reader.channel_count,) or not np.all(np.isfinite(factors)) or np.any(factors <= 0):
            raise ValueError('The recording has invalid probe multipliers.')
        values = to_volts(raw, reader.metadata, factors)
        if not np.all(np.isfinite(values)):
            raise ValueError('The recording has invalid voltage values.')
        note = f'Recorded voltage · nominal timing · {segment}'
        if count > MAX_REFERENCE_ROWS:
            note += ' · segment shortened to the reference preview limit'
        if not reader.metadata.get('complete'):
            note += ' · incomplete recording: committed samples only'
        return ReferenceData(Path(path).name, np.arange(len(raw))/rate, values, reader.channel_order, 'recording', note)
    finally:
        reader.close()


def load_vendor_reference(entry, arrays, title):
    values = np.column_stack([arrays[trace['npz_key']] for trace in entry['traces']]).astype(float)
    if not len(values) or not np.all(np.isfinite(values)):
        raise ValueError('This example has no valid waveform data.')
    return ReferenceData(title, np.linspace(0., 1., len(values)), values, list(range(1, values.shape[1]+1)),
                         'vendor', 'SHAPE ONLY · vendor sample interval and physical units are unverified')


def transformed_reference(data, mapping, offsets, anchor=0., shift_ms=0., stretch=1., gain=1., vertical_shift=0., shape_duration=5., shape_height=5.):
    parameters = [anchor, shift_ms, stretch, gain, vertical_shift, shape_duration, shape_height]
    if not np.all(np.isfinite(parameters)) or min(stretch, gain, shape_duration, shape_height) <= 0:
        raise ValueError('Reference scaling must be finite and positive.')
    x = anchor + shift_ms/1000 + data.x * stretch * (shape_duration if data.kind == 'vendor' else 1.)
    result = []
    for column, target in enumerate(mapping):
        if target is None:
            continue
        if target not in range(8) or column >= data.values.shape[1]:
            raise ValueError('Invalid reference channel mapping.')
        y = data.values[:, column]
        if data.kind == 'vendor':
            extent = float(np.ptp(y))
            y = (y-np.min(y))/extent if extent > 0 else np.zeros_like(y)
            y = y * shape_height
        result.append((column, target, x, y*gain + vertical_shift + offsets[target]))
    return result


def recent_references():
    try:
        values = json.loads((data_directory()/'reference-history.json').read_text())
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, str) and Path(value).is_absolute()][:12]
    except (OSError, ValueError, TypeError):
        return []


def remember_reference(path):
    path = str(Path(path).resolve())
    history = [path] + [item for item in recent_references() if item != path]
    target = data_directory()/'reference-history.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, prefix='.references-', delete=False) as file:
            temporary = Path(file.name)
            json.dump(history[:12], file, indent=2)
        temporary.replace(target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class ReferenceDialog(W.QDialog):
    def __init__(self, overlay, vendor_index=None):
        super().__init__(overlay.window)
        self.overlay = overlay
        self.result_data = None
        self.result_settings = None
        self.setWindowTitle('Reference overlay · TraceForge')
        self.setMinimumWidth(710)
        layout = W.QVBoxLayout(self)
        form = self.form = W.QFormLayout()
        self.kind = W.QComboBox()
        self.kind.addItems(['Your saved recording (.hscope)', 'Bundled vendor example · shape only', 'Image / screenshot · visual only'])
        form.addRow('Reference source', self.kind)
        self.files = W.QComboBox()
        self.files.setMinimumContentsLength(30)
        self.files.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for path in recent_references():
            if Path(path).suffix.lower() != '.hscope':
                continue
            self.files.addItem(Path(path).name, path)
        file_row = self.file_row = W.QHBoxLayout()
        file_row.addWidget(self.files, 1)
        browse = W.QPushButton('Browse…')
        browse.clicked.connect(self.browse)
        file_row.addWidget(browse)
        form.addRow('User recording', file_row)
        self.images = W.QComboBox()
        self.images.setMinimumContentsLength(30)
        self.images.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for path in recent_references():
            if Path(path).suffix.lower() != '.hscope':
                self.images.addItem(Path(path).name, path)
        self.image_row = W.QHBoxLayout()
        self.image_row.addWidget(self.images, 1)
        image_browse = W.QPushButton('Browse…')
        image_browse.clicked.connect(self.browse)
        self.image_row.addWidget(image_browse)
        form.addRow('Image file', self.image_row)
        self.examples = W.QComboBox()
        library = overlay.window.library
        for i in range(library.list.count()):
            item = library.list.item(i)
            self.examples.addItem(item.text(), item.data(QtCore.Qt.ItemDataRole.UserRole))
        form.addRow('Vendor example', self.examples)
        self.start = self.number(0, 10000000, 0, decimals=6, suffix=' s')
        self.duration = self.number(.000001, 600, min(600, overlay.window.time_window.currentData()), decimals=6, suffix=' s')
        self.frame = W.QSpinBox()
        self.frame.setRange(1, 100000000)
        self.frame.setToolTip('Fast-mode recordings use a single selected frame. Start and length apply to rolling recordings.')
        form.addRow('Recording start', self.start)
        form.addRow('Recording length', self.duration)
        form.addRow('Fast recording frame', self.frame)
        self.shift = self.number(-10000000, 10000000, 0, decimals=3, suffix=' ms')
        self.stretch = self.number(.000001, 1000000, 1, decimals=6, suffix=' ×')
        self.gain = self.number(.000001, 1000000, 1, decimals=6, suffix=' ×')
        self.vertical = self.number(-100000, 100000, 0, decimals=3, suffix=' V')
        self.shape_duration = self.number(.000001, 600, min(600, overlay.window.time_window.currentData()), decimals=6, suffix=' s')
        self.shape_height = self.number(.001, 100000, 5, decimals=3, suffix=' display V')
        form.addRow('Group time shift', self.shift)
        form.addRow('Group time stretch', self.stretch)
        form.addRow('Group voltage scale', self.gain)
        form.addRow('Group vertical shift', self.vertical)
        form.addRow('Vendor shape width', self.shape_duration)
        form.addRow('Vendor shape height', self.shape_height)
        self.image_width = self.number(.000001, 10000000, 1, decimals=6, suffix=' display s')
        self.image_height = self.number(.001, 1000000, 5, decimals=3, suffix=' display V')
        self.image_bottom = self.number(-1000000, 1000000, 0, decimals=3, suffix=' display V')
        form.addRow('Image width', self.image_width)
        form.addRow('Image height', self.image_height)
        form.addRow('Image bottom position', self.image_bottom)
        self.fit_image_button = W.QPushButton('Fit image to current graph')
        self.fit_image_button.clicked.connect(self.fit_image)
        form.addRow('', self.fit_image_button)
        self.fit_image()
        self.opacity = W.QSpinBox()
        self.opacity.setRange(10, 100)
        self.opacity.setValue(45)
        self.opacity.setSuffix(' %')
        form.addRow('Reference opacity', self.opacity)
        layout.addLayout(form)
        grid = W.QGridLayout()
        self.mappings = []
        self.mapping_labels = []
        for i in range(8):
            label = W.QLabel(f'Reference CH/trace {i+1} →')
            target = W.QComboBox()
            target.addItem('Hidden', None)
            for ch in range(8):
                hidden = '' if overlay.window.channel_checks[ch].isChecked() else ' (hidden)'
                target.addItem(f'Graph {overlay.window.channel_title(ch, physical=True)}{hidden}', ch)
            target.setCurrentIndex(i+1)
            grid.addWidget(label, i//2, (i%2)*2)
            grid.addWidget(target, i//2, (i%2)*2+1)
            self.mappings.append(target)
            self.mapping_labels.append(label)
        layout.addLayout(grid)
        note = self.note = W.QLabel()
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error = W.QLabel()
        self.error.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #ffb0a2')
        layout.addWidget(self.error)
        controls = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Apply | W.QDialogButtonBox.StandardButton.Cancel)
        controls.button(W.QDialogButtonBox.StandardButton.Apply).setText('Show overlay')
        controls.button(W.QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        settings = overlay.settings
        if settings:
            self.kind.setCurrentIndex({'recording': 0, 'vendor': 1, 'image': 2}[settings['kind']])
            if settings.get('path'):
                self.add_file(settings['path'])
            self.examples.setCurrentIndex(settings.get('vendor_index', 0))
            for key, control in self.controls().items():
                control.setValue(settings.get(key, control.value()))
            for i, target in settings.get('channel_mapping', {}).items():
                self.mappings[int(i)-1].setCurrentIndex(self.mappings[int(i)-1].findData(target))
        elif overlay.window.last_recording:
            self.add_file(str(overlay.window.last_recording))
        if vendor_index is not None:
            self.kind.setCurrentIndex(1)
            self.examples.setCurrentIndex(vendor_index)
        self.kind.currentIndexChanged.connect(self.update_kind)
        self.files.currentIndexChanged.connect(self.update_kind)
        self.images.currentIndexChanged.connect(self.update_kind)
        self.examples.currentIndexChanged.connect(self.update_kind)
        self.update_kind()

    @staticmethod
    def number(low, high, value, decimals, suffix):
        control = W.QDoubleSpinBox()
        control.setDecimals(decimals)
        control.setRange(low, high)
        control.setValue(value)
        control.setSuffix(suffix)
        control.setKeyboardTracking(False)
        return control

    def controls(self):
        return {'start_seconds': self.start, 'duration': self.duration, 'frame_number': self.frame,
                'shift_ms': self.shift, 'stretch': self.stretch, 'gain': self.gain,
                'vertical_shift': self.vertical, 'shape_duration': self.shape_duration,
                'shape_height': self.shape_height, 'opacity': self.opacity,
                'image_width': self.image_width, 'image_height': self.image_height, 'image_bottom': self.image_bottom}

    def fit_image(self):
        x, y = self.overlay.window.plot.viewRange()
        self.image_width.setValue(x[1]-x[0])
        self.image_height.setValue(y[1]-y[0])
        self.image_bottom.setValue(y[0])
        self.shift.setValue(0)

    def update_kind(self):
        vendor = self.kind.currentIndex() == 1
        image = self.kind.currentIndex() == 2
        recording = not vendor and not image
        channels = list(range(1, 9))
        names = {}
        fast = False
        if vendor:
            entry = self.examples.currentData()
            channels = list(range(1, entry['trace_count']+1)) if entry else []
        elif recording and self.files.currentData():
            self.files.setToolTip(self.files.currentData())
            try:
                reader = CaptureReader(self.files.currentData())
                try:
                    channels = reader.channel_order
                    names = normalize_names(reader.metadata.get('channel_names'))
                    fast = reader.mode == 'burst'
                    self.frame.setMaximum(max(1, len(reader.frames)) if fast else 100000000)
                finally:
                    reader.close()
            except Exception:
                pass
        self.form.setRowVisible(self.file_row, recording)
        self.form.setRowVisible(self.start, recording and not fast)
        self.form.setRowVisible(self.duration, recording and not fast)
        self.form.setRowVisible(self.frame, recording and fast)
        self.form.setRowVisible(self.image_row, image)
        for control in (self.image_width, self.image_height, self.image_bottom, self.fit_image_button):
            self.form.setRowVisible(control, image)
        for control in (self.stretch, self.gain, self.vertical):
            self.form.setRowVisible(control, not image)
        self.form.labelForField(self.shift).setText('Image horizontal shift' if image else 'Group time shift')
        self.images.setToolTip(self.images.currentData() or '')
        for control in (self.examples, self.shape_duration, self.shape_height):
            self.form.setRowVisible(control, vendor)
        for i, (label, control) in enumerate(zip(self.mapping_labels, self.mappings)):
            name = f' - {names[str(i+1)]}' if str(i+1) in names else ''
            label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            label.setText(f'Reference {"trace" if vendor else "CH"}{i+1}{name} →')
            label.setVisible(not image and i+1 in channels)
            control.setVisible(not image and i+1 in channels)
        self.note.setText(('Images appear behind the traces. Width, height and position place the picture on the graph; they do not calibrate its pixels or extract waveform data. Animated images show their first frame. ' if image else
                           'Reference traces are dashed. All reference channels share the same time alignment. ') +
                          'References never enter measurements or recordings. Loading holds the live display for comparison; acquisition and recording continue. Resume display to watch live data, without automatic trigger alignment.')

    def add_file(self, path):
        files = self.images if self.kind.currentIndex() == 2 else self.files
        index = files.findData(path)
        if index < 0:
            files.addItem(Path(path).name, path)
            index = files.count()-1
        files.setCurrentIndex(index)
        files.setToolTip(path)

    def browse(self):
        image = self.kind.currentIndex() == 2
        files = self.images if image else self.files
        folder = str(Path(files.currentData()).parent) if files.currentData() else str(recordings_directory())
        filters = 'Supported images (' + ' '.join('*.'+value for value in image_formats()) + ');;All files (*)' if image else 'TraceForge recordings (*.hscope)'
        path, _ = W.QFileDialog.getOpenFileName(self, 'Choose your reference image' if image else 'Choose your reference recording', folder, filters)
        if path:
            self.add_file(path)

    def apply(self):
        settings = {key: control.value() for key, control in self.controls().items()}
        settings.update(kind=('recording', 'vendor', 'image')[self.kind.currentIndex()],
                        path=(self.images if self.kind.currentIndex() == 2 else self.files).currentData(),
                        vendor_index=self.examples.currentIndex(),
                        channel_mapping={str(i+1): control.currentData() for i, control in enumerate(self.mappings)})
        try:
            if settings['kind'] == 'recording':
                if not settings['path']:
                    raise ValueError('Choose a saved .hscope recording using Browse.')
                data = load_recording_reference(settings['path'], settings['start_seconds'], settings['duration'], settings['frame_number']-1)
            elif settings['kind'] == 'image':
                if not settings['path']:
                    raise ValueError('Choose an image using Browse.')
                data = load_image_reference(settings['path'])
            else:
                data = load_vendor_reference(self.examples.currentData(), self.overlay.window.library.arrays, self.examples.currentText())
            if data.kind != 'image' and not any(settings['channel_mapping'][str(ch)] is not None for ch in data.channels):
                raise ValueError('Map at least one reference channel to a graph channel.')
        except Exception as exc:
            self.error.setText(f'Could not load reference: {exc}')
            return
        self.result_data, self.result_settings = data, settings
        self.accept()


class ReferenceOverlay:
    def __init__(self, window, colors):
        self.window, self.colors = window, colors
        self.data = None
        self.settings = {}
        self.curves = []
        self.image_item = None
        self.bar = W.QWidget()
        layout = W.QHBoxLayout(self.bar)
        layout.setContentsMargins(0, 0, 0, 0)
        load = W.QPushButton('Reference overlay…')
        load.clicked.connect(lambda: self.configure())
        layout.addWidget(load)
        self.visible = W.QCheckBox('Show reference')
        self.visible.setEnabled(False)
        self.visible.toggled.connect(self.render)
        layout.addWidget(self.visible)
        self.summary = W.QLabel('Load a recording, image, or bundled example.')
        self.summary.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet('font-size: 11px; color: #a9bdcd')
        layout.addWidget(self.summary, 1)
        self.fit_button = W.QPushButton('Fit reference')
        self.fit_button.clicked.connect(self.fit)
        self.fit_button.setEnabled(False)
        layout.addWidget(self.fit_button)
        clear = W.QPushButton('Clear')
        clear.clicked.connect(self.clear)
        layout.addWidget(clear)
        window.plot.getViewBox().sigXRangeChanged.connect(self.render)

    def configure(self, vendor_index=None):
        dialog = ReferenceDialog(self, vendor_index)
        if dialog.exec() != W.QDialog.DialogCode.Accepted:
            return
        self.install(dialog.result_data, dialog.result_settings)
        if self.settings['kind'] in ('recording', 'image'):
            try:
                remember_reference(self.settings['path'])
            except OSError:
                self.summary.setText(self.summary.text() + ' · recent-file list could not be saved')

    def install(self, data, settings):
        if data.kind == 'image':
            dimensions = [settings[key] for key in ('image_width', 'image_height', 'image_bottom', 'shift_ms')]
            if not np.all(np.isfinite(dimensions)) or min(dimensions[:2]) <= 0:
                raise ValueError('Image size must be positive and its position finite.')
        self.clear()
        self.data, self.settings = data, dict(settings)
        if data.kind == 'image':
            self.image_item = W.QGraphicsPixmapItem(QtGui.QPixmap.fromImage(data.image))
            self.image_item.setTransformationMode(QtCore.Qt.TransformationMode.SmoothTransformation)
            self.image_item.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            self.image_item.setZValue(-10)
            self.window.plot.addItem(self.image_item, ignoreBounds=True)
        else:
            for column in range(data.values.shape[1]):
                curve = pg.PlotDataItem(autoDownsample=True, downsampleMethod='peak', clipToView=True)
                curve.setZValue(-1)
                self.window.plot.addItem(curve, ignoreBounds=True)
                self.curves.append(curve)
        self.visible.setEnabled(True)
        self.fit_button.setEnabled(True)
        self.visible.setChecked(True)
        if self.window.active() and not self.window.held:
            self.window.hold_button.setChecked(True)
            self.window.toggle_hold(True)
        self.window.tabs.setCurrentIndex(0)
        self.render()

    def render(self, *_):
        if self.data is None:
            return
        if self.data.kind == 'image':
            width, height, bottom = (self.settings[key] for key in ('image_width', 'image_height', 'image_bottom'))
            left = self.window.plot.viewRange()[0][0] + self.settings['shift_ms']/1000
            self.image_item.setTransform(QtGui.QTransform(width/self.data.image.width(), 0, 0,
                                                         -height/self.data.image.height(), left, bottom+height))
            self.image_item.setOpacity(self.settings['opacity']/100)
            self.image_item.setVisible(self.visible.isChecked())
            state = 'shown' if self.visible.isChecked() else 'hidden'
            self.summary.setText(f'IMAGE · visual only: {self.data.title} · {state}')
            self.summary.setToolTip(self.data.note + '\nImage position is independent of channel handles. Use Hold for stable comparison.')
            return
        mapping = [self.settings['channel_mapping'].get(str(ch)) for ch in self.data.channels]
        params = {key: self.settings[key] for key in ('shift_ms', 'stretch', 'gain', 'vertical_shift', 'shape_duration', 'shape_height')}
        arrays = transformed_reference(self.data, mapping, self.window.channel_offsets, self.window.plot.viewRange()[0][0], **params)
        for curve in self.curves:
            curve.hide()
        shown = 0
        for column, target, x, y in arrays:
            curve = self.curves[column]
            color = pg.mkColor(self.colors[target])
            color.setAlphaF(self.settings['opacity']/100)
            curve.setPen(pg.mkPen(color, width=2, style=QtCore.Qt.PenStyle.DashLine))
            curve.setData(x, y, symbol='o' if len(x) <= 50 else None, symbolSize=4, symbolBrush=color, symbolPen=None)
            enabled = self.visible.isChecked() and self.window.channel_checks[target].isChecked()
            curve.setVisible(enabled)
            shown += int(enabled)
        scaled = self.settings['stretch'] != 1 or self.settings['gain'] != 1
        label = 'SHAPE ONLY · unverified units/time' if self.data.kind == 'vendor' else 'SCALED REFERENCE' if scaled else 'REFERENCE'
        self.summary.setText(f'{label}: {self.data.title} · dashed · {shown} shown')
        if self.visible.isChecked() and not shown:
            self.summary.setText(self.summary.text() + ' · enable a mapped graph channel')
        self.summary.setToolTip(self.data.note + f"\nTime shift: {self.settings['shift_ms']:g} ms · time stretch: {self.settings['stretch']:g}× · voltage scale: {self.settings['gain']:g}×\nReference traces follow their mapped channel positions. Use Hold for stable comparison.")

    def fit(self):
        values = [curve.getData()[1] for curve in self.curves + self.window.curves if curve.isVisible() and curve.getData()[1] is not None and len(curve.getData()[1])]
        if self.image_item is not None and self.image_item.isVisible():
            bottom = self.settings['image_bottom']
            values.append(np.array([bottom, bottom+self.settings['image_height']]))
        if not values:
            return
        low = min(float(np.min(value)) for value in values)
        high = max(float(np.max(value)) for value in values)
        pad = max(.08, (high-low)*.15)
        self.window.voltage_span.setCurrentIndex(self.window.voltage_span.findData('manual'))
        self.window.plot.setYRange(low-pad, high+pad, padding=0)

    def clear(self):
        if self.image_item is not None:
            self.window.plot.removeItem(self.image_item)
            self.image_item = None
        for curve in self.curves:
            self.window.plot.removeItem(curve)
        self.curves = []
        self.data = None
        self.settings = {}
        self.visible.setChecked(False)
        self.visible.setEnabled(False)
        self.fit_button.setEnabled(False)
        self.summary.setText('Load a recording, image, or bundled example.')
        self.summary.setToolTip('')
