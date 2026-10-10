#!/usr/bin/env python3
"""TraceForge oscilloscope workbench for Linux."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from html import escape
from pathlib import Path
import signal
import sys
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets as W

from acquisition import Acquisition
from paths import (recordings_directory, default_recordings_directory,
                   set_recordings_directory, new_recording_path)
from usb_setup import usb_configured, setup_complete, show_usb_setup
from shortcuts import ensure_shortcuts
from device_detection import scan_device
from reference_overlay import ReferenceOverlay
from channel_names import MAX_NAME_LENGTH, clean_name, normalize_names
from video_export import VideoDialog
from core import (RATE, TIME_DIVISIONS, HISTORY_SECONDS, CaptureReader,
                  SampleBuffer, FrameBuffer, format_time, to_volts,
                  channel_order, scan_channels, nominal_rate)

ROOT = Path(__file__).resolve().parent
VERSION = "0.7.5"
COLORS = ["#f5c65a", "#62bbff", "#ee809f", "#77ddac", "#bf9bff", "#ff9e68", "#69dce6", "#d7daea"]
READOUT_FILTERS = [('OFF', 3), ('LOW', 2), ('HIGH', 1)]

STYLE = """
QMainWindow, QWidget { background: #14191f; color: #e1e8f0; font-family: 'Noto Sans'; font-size: 12px; }
QLabel#title { font-size: 24px; font-weight: 700; }
QLabel#subtitle, QLabel#muted { color: #96a8b9; }
QLabel#status { border-radius: 7px; padding: 8px 15px; background: #273340; font-weight: 700; }
QPushButton { background: #283440; border: 1px solid #3b4b5b; border-radius: 6px; padding: 8px 13px; }
QPushButton:hover { background: #364958; border-color: #6d93b0; }
QPushButton:disabled { color: #687786; background: #1c252e; border-color: #2d3742; }
QPushButton#run { background: #23624f; border-color: #36896e; font-weight: 700; }
QPushButton#record { color: #ffada7; }
QPushButton:checked { background: #405568; border-color: #88badd; }
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox { background: #212c37; border: 1px solid #3b4b5b; border-radius: 5px; padding: 5px 8px; }
QComboBox QAbstractItemView { background: #212c37; color: #e1e8f0; selection-background-color: #3d5365; }
QTabWidget::pane { border: 1px solid #303d49; border-radius: 5px; }
QTabBar::tab { padding: 11px 23px; color: #9aaebe; background: #19222a; }
QTabBar::tab:selected { color: #e3edf7; background: #2b3946; border-bottom: 2px solid #7dd7b4; }
QGroupBox { border: 1px solid #35424f; border-radius: 8px; margin-top: 13px; padding-top: 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #aec0cf; }
QFrame#card { background: #1e2832; border: 1px solid #35424f; border-radius: 7px; }
QFrame#card QLabel, QFrame#card QCheckBox { background: transparent; }
QLabel#measurement { font-size: 22px; font-weight: 650; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; }
QListWidget { background: #19232d; border: 1px solid #344350; border-radius: 5px; padding: 4px; }
QListWidget::item { padding: 8px 5px; }
QListWidget::item:selected { background: #344c5c; }
QStatusBar { color: #9bb0c3; }
QSlider::groove:horizontal { height: 5px; background: #354655; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; background: #7cceb1; margin: -5px 0; border-radius: 5px; }
QToolTip { background: #263a4b; color: #edf5fb; border: 1px solid #688297; padding: 6px; }
"""


def muted(text):
    widget = W.QLabel(text)
    widget.setObjectName("muted")
    widget.setWordWrap(True)
    return widget


def button(text, callback, name=None):
    widget = W.QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def plot_widget(y_label, x_label, x_units=None):
    widget = pg.PlotWidget(background="#10171e")
    widget.showGrid(x=True, y=True, alpha=0.22)
    widget.setLabel("left", y_label)
    widget.setLabel("bottom", x_label, units=x_units)
    widget.getPlotItem().hideButtons()
    widget.getPlotItem().setMenuEnabled(False)
    widget.setMouseEnabled(x=True, y=True)
    for axis in ("left", "bottom"):
        widget.getAxis(axis).setPen("#708697")
        widget.getAxis(axis).setTextPen("#a9bdcd")
    return widget


REFERENCE_NAMES = {
    "AM_ACTUATORS_CCSV": "Carbon canister solenoid voltage",
    "AM_ACTUATORS_IP_MIV": "Injector · multipoint voltage",
    "AM_SENSORS_C_HE": "Camshaft sensor · Hall effect",
    "AM_SENSORS_C_I_RC_R": "Crankshaft sensor · inductive / running",
    "AM_IGNITION_P_P": "Primary ignition voltage",
    "AM_CS_CC_ACRDT": "Charging · alternator ripple",
    "AM_CS_RC_PETROL": "Relative compression · petrol",
}


class ReferenceLibrary(W.QWidget):
    overlayRequested = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        layout = W.QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        left = W.QVBoxLayout()
        self.search = W.QLineEdit()
        self.search.setPlaceholderText("Filter names or original filenames…")
        self.search.setMinimumWidth(310)
        self.search.textChanged.connect(self.filter_items)
        self.list = W.QListWidget()
        self.list.setMinimumWidth(340)
        self.list.currentItemChanged.connect(self.select_item)
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        left.addWidget(button("Overlay selected example…", lambda: self.overlayRequested.emit(self.list.currentRow())))
        self.count = muted("")
        left.addWidget(self.count)
        layout.addLayout(left, 1)
        right = W.QVBoxLayout()
        self.title = W.QLabel("Vendor reference waveforms")
        self.title.setStyleSheet("font-size: 20px; font-weight: 650")
        self.info = muted("Recovered from your Windows installer")
        self.plot = plot_widget("Stored value · units unverified", "Sample index")
        self.plot.addLegend(offset=(12, 12))
        right.addWidget(self.title)
        right.addWidget(self.info)
        right.addWidget(self.plot, 1)
        right.addWidget(muted("Reference examples from Hantek's Windows package. Sample intervals and physical units have not been verified; these traces use their original stored values and a sample-index axis."))
        layout.addLayout(right, 3)
        self.arrays = np.load(ROOT / "references/reference-waveforms.npz", allow_pickle=False)
        entries = json.loads((ROOT / "references/reference-index.json").read_text())
        self.entries = [e for e in entries if e.get("status") == "decoded"]
        for entry in self.entries:
            stem = Path(entry["source_filename"]).stem
            title = REFERENCE_NAMES.get(stem, stem.removeprefix("AM_").replace("_", " · "))
            item = W.QListWidgetItem(title)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry["source_filename"])
            self.list.addItem(item)
        self.filter_items("")
        self.list.setCurrentRow(0)

    def filter_items(self, query):
        visible = 0
        for i in range(self.list.count()):
            item = self.list.item(i)
            entry = item.data(QtCore.Qt.ItemDataRole.UserRole)
            matches = query.lower() in (item.text() + " " + entry["source_filename"]).lower()
            item.setHidden(not matches)
            visible += matches
        self.count.setText(f"{visible} of {len(self.entries)} decoded references · 121 traces total")

    def select_item(self, item, previous=None):
        if item is None:
            return
        entry = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self.title.setText(item.text())
        self.info.setText(f"{entry['source_filename']}   ·   {entry['trace_count']} trace(s)   ·   {entry['samples_per_trace']:,} samples per trace")
        self.plot.clear()
        for n, trace in enumerate(entry["traces"]):
            values = self.arrays[trace["npz_key"]]
            curve = self.plot.plot(np.arange(len(values)), values, pen=pg.mkPen(COLORS[n % 8], width=1.6),
                                   name=f"Trace {n+1}", autoDownsample=True)
            
            
            curve.setClipToView(True)
        self.plot.enableAutoRange()


class MainWindow(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"TraceForge {VERSION} · for Linux")
        self.resize(1480, 1000)
        self.setMinimumSize(1110, 780)
        self.buffer = SampleBuffer()
        self.metadata = None
        self.worker = None
        self.reader = None
        self.closing = False
        self.recording_active = False
        self.recording_pending = False
        self.recording_requested = False
        self.last_recording = None
        self.recording_name = None
        self.recording_part = 0
        self.last_error = None
        self.detection = None
        self.held = False
        self.displayed = (0, np.empty((0, 8), dtype="<u2"))
        self.view_samples = self.displayed
        self.last_total = -1
        self.first_data = True
        self.last_readout_key = None
        self.noise_filter_index = 0
        self.readout_values = {}
        self.readout_ranges = {}
        self.live_visible_channels = list(range(8))
        self.live_channel_names = {}
        self.channel_names = dict(self.live_channel_names)
        self.channel_offsets = np.zeros(8, dtype=float)
        self.auto_stack_pending = True
        self.restart_pending = False
        self.resume_recording = False
        self.reconfigure_timer = QtCore.QTimer(self)
        self.reconfigure_timer.setSingleShot(True)
        self.reconfigure_timer.setInterval(300)
        self.reconfigure_timer.timeout.connect(self.apply_configuration)
        self.build_ui()
        self.apply_channel_names(self.channel_names)
        self.configure_time_controls("roll")
        self.update_channel_cards()
        self.stack_traces(automatic=True)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        
        self.timer.start(80)
        
        
        self.readout_timer = QtCore.QTimer(self)
        self.readout_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.readout_timer.timeout.connect(self.update_latest_readouts)
        self.readout_timer.start(20)

    def build_ui(self):
        central = W.QWidget()
        self.setCentralWidget(central)
        outer = W.QVBoxLayout(central)
        outer.setContentsMargins(22, 20, 22, 10)
        outer.setSpacing(14)
        header = W.QHBoxLayout()
        titles = W.QVBoxLayout()
        title = W.QLabel("TraceForge")
        title.setObjectName("title")
        titles.addWidget(title)
        subtitle = W.QLabel("for Linux · Hantek 1008 series")
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.mode_combo = W.QComboBox()
        self.mode_combo.addItem("Rolling · s / min", "roll")
        self.mode_combo.addItem("Fast frames · µs / ms", "burst")
        self.mode_combo.setToolTip("Time / division automatically selects the capture mode. Fast frames have gaps between captures.")
        header.addWidget(self.mode_combo)
        self.badge = W.QLabel("READY")
        self.badge.setObjectName("status")
        header.addWidget(self.badge)
        self.run_button = button("▶  Connect + run", self.toggle_run, "run")
        self.run_button.setMinimumWidth(160)
        header.addWidget(self.run_button)
        self.record_button = button("●  Record", self.toggle_record, "record")
        self.record_button.setEnabled(False)
        header.addWidget(self.record_button)
        self.usb_setup_button = button("USB setup", self.usb_setup)
        header.addWidget(self.usb_setup_button)
        header.addWidget(button("Shortcuts", lambda: ensure_shortcuts(self, force=True)))
        header.addWidget(button("About", self.about))
        outer.addLayout(header)

        device_row = W.QHBoxLayout()
        self.device_label = W.QLabel("USB device check pending")
        self.device_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.device_label.setWordWrap(True)
        device_row.addWidget(self.device_label, 1)
        self.noise_filter_button = button('Noise filter: OFF', self.cycle_noise_filter)
        self.noise_filter_button.setMinimumWidth(160)
        self.noise_filter_button.setToolTip('Click to cycle OFF (1.234 V), LOW (1.23 V), HIGH (1.2 V). Rounds voltage readouts. Enable Apply filter to graph to also round the displayed traces. Recordings and exports keep their precision.')
        device_row.addWidget(self.noise_filter_button)
        self.graph_filter_button = button('Apply filter to graph: OFF', self.toggle_graph_filter)
        self.graph_filter_button.setCheckable(True)
        self.graph_filter_button.setMinimumWidth(205)
        self.graph_filter_button.setToolTip('Round displayed channel traces using LOW (0.01 V) or HIGH (0.1 V). Noise filter OFF shows the original waveform. Raw recordings and exports are always unfiltered.')
        device_row.addWidget(self.graph_filter_button)
        self.detect_button = button("Detect device", self.check_device)
        device_row.addWidget(self.detect_button)
        outer.addLayout(device_row)

        self.tabs = W.QTabWidget()
        outer.addWidget(self.tabs, 1)
        live = W.QWidget()
        self.tabs.addTab(live, "Live scope / recordings")
        layout = W.QHBoxLayout(live)
        layout.setContentsMargins(16, 16, 16, 16)
        plot_column = W.QVBoxLayout()
        toolbar = W.QHBoxLayout()
        self.hold_button = button("Hold display", self.toggle_hold)
        self.hold_button.setCheckable(True)
        self.hold_button.setToolTip("Freeze the view; acquisition and recording continue. Stop releases USB.")
        toolbar.addWidget(self.hold_button)
        toolbar.addWidget(button("Fit voltage", self.fit_voltage))
        toolbar.addWidget(button("Stack traces", self.stack_traces))
        toolbar.addWidget(button("Reset positions", self.reset_trace_positions))
        self.cursor_button = button("Cursors", self.toggle_cursors)
        self.cursor_button.setCheckable(True)
        toolbar.addWidget(self.cursor_button)
        toolbar.addStretch()
        self.export_button = button("Export view…", self.export_view_dialog)
        self.export_button.setText('Export…')
        self.export_button.clicked.disconnect()
        export_menu = W.QMenu(self.export_button)
        export_menu.addAction('Displayed samples as CSV…', self.export_view_dialog)
        self.video_action = export_menu.addAction('Entire recording as video…', self.export_video_dialog)
        self.video_action.setEnabled(False)
        self.export_button.setMenu(export_menu)
        toolbar.addWidget(self.export_button)
        self.open_button = button("Open recording…", self.open_dialog)
        toolbar.addWidget(self.open_button)
        plot_column.addLayout(toolbar)
        self.plot = plot_widget("Voltage", "Time from acquisition start · nominal", "s")
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.setYRange(-0.4, 2.1, padding=0)
        self.plot.setXRange(0, 5, padding=0)
        self.legend = self.plot.addLegend(offset=(12, 12), colCount=2)
        self.curves = [self.plot.plot([], [], pen=pg.mkPen(color, width=1), name=f"CH {i+1}",
                                      autoDownsample=True, downsampleMethod="peak", clipToView=True)
                       for i, color in enumerate(COLORS)]
        self.channel_baselines = []
        for i, color in enumerate(COLORS):
            baseline = pg.InfiniteLine(pos=0, angle=0, movable=True, bounds=(-100000, 100000),
                                       pen=pg.mkPen(color, width=1.5), hoverPen=pg.mkPen(color, width=3),
                                       span=(.008, .035), markers=[('^', .3, 24)], label=f"CH{i+1} · — V",
                                       labelOpts={'position': 1, 'anchors': [(0, .5), (0, .5)],
                                                  'color': color, 'fill': '#14191f'})
            baseline.setZValue(15)
            baseline.setToolTip(f"Drag CH{i+1}'s triangle up or down. Only the trace position changes.")
            baseline.sigPositionChanged.connect(lambda line, n=i: self.set_channel_offset(n, line.value()))
            self.plot.addItem(baseline, ignoreBounds=True)
            self.channel_baselines.append(baseline)
        self.cursors = [pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#a9c3d5", width=1, style=QtCore.Qt.PenStyle.DashLine)),
                        pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("#a9c3d5", width=1, style=QtCore.Qt.PenStyle.DashLine)),
                        pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen("#8296a8", width=1, style=QtCore.Qt.PenStyle.DashLine)),
                        pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen("#8296a8", width=1, style=QtCore.Qt.PenStyle.DashLine))]
        for cursor in self.cursors:
            self.plot.addItem(cursor)
            cursor.hide()
            cursor.sigPositionChanged.connect(self.update_cursors)
        self.reference_overlay = ReferenceOverlay(self, COLORS)
        plot_column.addWidget(self.reference_overlay.bar)
        plot_column.addWidget(self.plot, 1)
        self.cursor_readout = muted("Cursors measure Δt, 1/Δt and ΔV. The timebase remains nominal.")
        plot_column.addWidget(self.cursor_readout)
        playback = W.QHBoxLayout()
        self.playback_label = muted("Recent 120 seconds retained in memory; Record saves continuously.")
        self.playback_slider = W.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.playback_slider.setRange(0, 10000)
        self.playback_slider.valueChanged.connect(lambda _: self.render_playback())
        self.playback_slider.hide()
        playback.addWidget(self.playback_label)
        playback.addWidget(self.playback_slider, 1)
        plot_column.addLayout(playback)
        layout.addLayout(plot_column, 1)

        side_content = W.QWidget()
        side = W.QVBoxLayout(side_content)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(8)
        side.setSizeConstraint(W.QLayout.SizeConstraint.SetMinimumSize)
        channel_box = W.QGroupBox("CHANNEL VISIBILITY / POSITION")
        channel_box.setFixedWidth(374)
        grid = W.QGridLayout(channel_box)
        grid.setSpacing(8)
        self.channel_checks, self.channel_values, self.channel_stats, self.probes = [], [], [], []
        self.position_controls = []
        self.name_controls = []
        for i, color in enumerate(COLORS):
            card = W.QFrame()
            card.setObjectName("card")
            card_layout = W.QVBoxLayout(card)
            card_layout.setContentsMargins(10, 4, 10, 4)
            card_layout.setSpacing(2)
            name_row = W.QHBoxLayout()
            name_row.setSpacing(4)
            identity = W.QLabel(f"CH{i+1} -")
            identity.setStyleSheet(f"color: {color}; font-weight: 700")
            name_row.addWidget(identity)
            name = W.QLineEdit()
            name.setObjectName(f"channelName{i+1}")
            name.setAccessibleName(f"CH{i+1} name")
            name.setPlaceholderText('Name (optional)')
            name.setMaxLength(MAX_NAME_LENGTH)
            name.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Fixed)
            name.setStyleSheet(f"color: {color}; font-size: 11px; padding: 3px 4px")
            name.textChanged.connect(lambda text, n=i: self.set_channel_name(n, text))
            name.editingFinished.connect(self.commit_channel_names)
            name_row.addWidget(name, 1)
            card_layout.addLayout(name_row)
            self.name_controls.append(name)
            top = W.QHBoxLayout()
            check = W.QCheckBox("ON")
            check.setChecked(True)
            check.setStyleSheet(f"color: {color}; font-weight: 700")
            check.toggled.connect(lambda visible, n=i: self.set_channel_visible(n, visible))
            top.addWidget(check)
            probe = W.QComboBox()
            probe.addItems(["1×", "10×", "20×", "100×"])
            probe.setMaximumWidth(72)
            probe.setMinimumHeight(28)
            probe.setToolTip("Display multiplier: match the physical probe/attenuator. Does not change input protection or the hardware range.")
            probe.currentIndexChanged.connect(self.probes_changed)
            top.addWidget(probe)
            card_layout.addLayout(top)
            value = W.QLabel("— V")
            value.setToolTip("Live: newest received sample, without averaging. Hold/replay: last sample in the displayed window.")
            value.setObjectName("measurement")
            value.setStyleSheet(f"color: {color}")
            value.ensurePolished()
            value.setMinimumHeight(value.sizeHint().height())
            stats = muted("min —   max —")
            stats.setStyleSheet("font-size: 10px; color: #a2b3c2")
            stats.setMinimumHeight(14)
            card_layout.addWidget(value)
            card_layout.addWidget(stats)
            position_row = W.QHBoxLayout()
            position_row.setSpacing(4)
            position_row.addWidget(W.QLabel("Position"))
            position = W.QDoubleSpinBox()
            position.setObjectName(f"channelPosition{i+1}")
            position.setRange(-100000, 100000)
            position.setDecimals(3)
            position.setSingleStep(.5)
            position.setSuffix(" V")
            position.setKeyboardTracking(False)
            position.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Fixed)
            position.setToolTip("Vertical display offset. Measured voltage and saved samples stay unchanged. You can also drag the colored zero marker on the graph.")
            position.setStyleSheet("font-size: 10px; padding: 3px 4px")
            position.valueChanged.connect(lambda value, n=i: self.set_channel_offset(n, value))
            position_row.addWidget(position, 1)
            card_layout.addLayout(position_row)
            self.position_controls.append(position)
            grid.addWidget(card, i // 2, i % 2)
            self.channel_checks.append(check)
            self.channel_values.append(value)
            self.channel_stats.append(stats)
            self.probes.append(probe)
        presets = W.QHBoxLayout()
        for label, channels in [("All on", list(range(8))), ("All off", [])]:
            control = button(label, lambda checked=False, selected=channels: self.select_channels(selected))
            control.setStyleSheet("padding: 6px 8px")
            presets.addWidget(control)
        grid.addLayout(presets, 4, 0, 1, 2)
        self.channel_note = muted("")
        grid.addWidget(self.channel_note, 5, 0, 1, 2)
        side.addWidget(channel_box)

        display_box = W.QGroupBox("DISPLAY")
        form = W.QFormLayout(display_box)
        form.setContentsMargins(12, 16, 12, 12)
        self.time_window = W.QComboBox()
        for seconds in [1, 2, 5, 10, 30, 60, 120]:
            self.time_window.addItem(f"{seconds} seconds", seconds)
        self.time_window.setCurrentIndex(2)
        self.time_window.currentIndexChanged.connect(self.window_changed)
        self.time_window_label = W.QLabel("Time window")
        form.addRow(self.time_window_label, self.time_window)
        self.frame_position_label = W.QLabel("Within frame")
        self.frame_position = W.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.frame_position.setRange(0, 10000)
        self.frame_position.setValue(5000)
        self.frame_position.setToolTip("Pan the microsecond view through the captured frame")
        self.frame_position.valueChanged.connect(lambda _: self.draw(*self.displayed))
        form.addRow(self.frame_position_label, self.frame_position)
        self.frame_position_label.hide()
        self.frame_position.hide()
        self.voltage_span = W.QComboBox()
        self.voltage_span.addItem("Auto fit visible channels", None)
        self.voltage_span.addItem("Manual · trace positions", "manual")
        for span in [0.2, 0.5, 1, 2, 5, 10, 20, 40, 100, 200]:
            self.voltage_span.addItem(f"{span:g} V full height", span)
        self.voltage_span.currentIndexChanged.connect(self.set_voltage_span)
        form.addRow("Voltage span", self.voltage_span)
        side.insertWidget(0, display_box)
        self.acquisition_note = muted("440 Sa/s per channel · rolling acquisition\nNominal sample spacing: 2.273 ms\nHardware gain: 1.0 · internal zero at connection")
        side.addWidget(self.acquisition_note)
        side.addStretch()
        side.addWidget(muted("Measurement validation details are in About."))
        side_scroll = W.QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        side_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side_scroll.setFixedWidth(374 + side_scroll.verticalScrollBar().sizeHint().width())
        side_scroll.setWidget(side_content)
        layout.addWidget(side_scroll)
        self.library = ReferenceLibrary()
        self.library.overlayRequested.connect(self.reference_overlay.configure)
        self.tabs.addTab(self.library, "Vendor reference library · 86")
        recording_location = W.QHBoxLayout()
        recording_location.addWidget(button("Recording folder…", self.choose_recording_directory))
        self.recording_location_label = muted("")
        self.recording_location_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.recording_location_label.setWordWrap(True)
        recording_location.addWidget(self.recording_location_label, 1)
        outer.addLayout(recording_location)
        self.update_recording_location()
        self.message = muted("Connect to begin. First-time USB access can be configured with USB setup.")
        outer.addWidget(self.message)
        self.statusBar().showMessage("Ready  ·  USB 0783:5725  ·  Python / Qt / PyQtGraph")
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)

    def burst_view(self):
        return self.metadata is not None and self.metadata.get("acquisition_mode") == "burst"

    def mode_changed(self, *_):
        if self.reader:
            self.reader.close()
            self.reader = None
            self.metadata = None
            self.displayed = (0, np.empty((0, 8), dtype="<u2"))
            self.view_samples = self.displayed
            self.restore_live_visibility()
            self.playback_slider.hide()
        target = 5e-4 if self.mode_combo.currentData() == "burst" else 5.0
        self.time_window.setCurrentIndex(self.time_window.findData(target))
        self.update_channel_cards()

    def configure_time_controls(self, mode):
        previous = self.time_window.currentData()
        self.time_window.blockSignals(True)
        self.time_window.clear()
        for division in TIME_DIVISIONS:
            self.time_window.addItem(f"{format_time(division)}/div · {format_time(division*10)} total", division*10)
        default = 5e-4 if mode == "burst" else 5.0
        selected = previous if previous and ((previous < 1) == (mode == "burst")) else default
        self.time_window.setCurrentIndex(self.time_window.findData(selected))
        self.time_window.blockSignals(False)
        self.time_window_label.setText("Time / division")
        self.update_time_axis()
        self.update_acquisition_note()
        self.last_total = -1

    def capture_configuration(self):
        division = self.time_window.currentData()/10
        mode = "burst" if division < .1 else "roll"
        ns = max(100_000, round(division*1e9)) if mode == "burst" else 100_000
        return mode, list(range(8)), ns

    def sample_rate(self):
        return float(self.metadata["sample_rate_nominal_hz"]) if self.metadata else RATE

    def recorded_channels(self):
        return channel_order(self.metadata) if self.metadata else list(range(1, 9))

    def update_time_axis(self):
        span = self.time_window.currentData()
        division = span/10
        scale, unit = ((1/60, "min") if division >= 60 else (1, "s") if division >= 1
                       else (1e3, "ms") if division >= .001 else (1e6, "µs"))
        fast = self.burst_view() if self.reader else span < 1
        self.frame_position_label.setVisible(fast)
        self.frame_position.setVisible(fast)
        axis = self.plot.getAxis("bottom")
        
        
        
        self.plot.setLabel("bottom", "Time within frame · nominal" if fast else "Time from acquisition start · nominal", units=unit, siPrefixEnableRanges=())
        axis.enableAutoSIPrefix(False)
        axis.setScale(scale)
        axis.setTickSpacing(major=division*scale, minor=division*scale/5)
        self.plot.setXRange(0, span, padding=0)
        self.playback_label.setText("Latest hardware frame; use Within frame to pan." if fast else "Recent 10 minutes retained; Record saves continuously.")

    def update_acquisition_note(self):
        if self.metadata:
            mode = self.metadata.get("acquisition_mode", "roll")
            rate = self.sample_rate()
            channels = self.recorded_channels()
            padding = self.metadata.get("padding_channels_discarded", [])
        else:
            mode, selected, ns = self.capture_configuration()
            rate = nominal_rate(mode, selected, ns)
            channels = [ch+1 for ch in selected]
            padding = [ch+1 for ch in scan_channels(selected, mode) if ch not in selected]
        speed = f"{rate/1e6:.5g} MSa/s" if rate >= 1e6 else f"{rate/1e3:.5g} kSa/s" if rate >= 1000 else f"{rate:g} Sa/s"
        note = f"{'Fast frames' if mode == 'burst' else 'Rolling'} · {speed}/ch nominal\nSample spacing: {format_time(1/rate)}\nInputs: " + ", ".join(f"CH{ch}" for ch in channels)
        if padding:
            note += "\nHardware padding: CH" + ", CH".join(map(str, padding)) + " (discarded)."
        note += "\nIndependent frames; transfer gaps are not plotted." if mode == "burst" else "\nHardware gain 1.0 · startup internal zero."
        self.acquisition_note.setText(note)

    def probe_factors(self):
        return [int(self.probes[ch-1].currentText().removesuffix("×")) for ch in self.recorded_channels()]

    def set_channel_visible(self, index, visible):
        self.curves[index].setVisible(visible)
        if not self.reader:
            self.live_visible_channels = [i for i, check in enumerate(self.channel_checks) if check.isChecked()]
        self.update_channel_cards()
        if visible:
            self.stack_traces(automatic=True)
        else:
            self.draw(*self.displayed)

    def select_channels(self, channels):
        available = set(self.recorded_channels()) if self.reader else set(range(1, 9))
        for i, check in enumerate(self.channel_checks):
            check.blockSignals(True)
            check.setChecked(i in channels and i+1 in available)
            check.blockSignals(False)
        if not self.reader:
            self.live_visible_channels = [i for i, check in enumerate(self.channel_checks) if check.isChecked()]
        self.update_channel_cards()
        self.stack_traces(automatic=True)

    def restore_live_visibility(self):
        self.apply_channel_names(self.live_channel_names)
        for i, check in enumerate(self.channel_checks):
            check.blockSignals(True)
            check.setChecked(i in self.live_visible_channels)
            check.blockSignals(False)
        self.auto_stack_pending = True

    def channel_title(self, index, physical=False):
        name = self.channel_names.get(str(index+1), '')
        return f'CH{index+1} - {name}' if name and physical else name or f'CH{index+1}'

    def apply_channel_names(self, names):
        self.channel_names = normalize_names(names)
        for i, control in enumerate(self.name_controls):
            control.blockSignals(True)
            control.setText(self.channel_names.get(str(i+1), ''))
            control.setCursorPosition(0)
            control.blockSignals(False)
            self.update_channel_label(i)

    def set_channel_name(self, index, text):
        name = clean_name(text)
        if name:
            self.channel_names[str(index+1)] = name
        else:
            self.channel_names.pop(str(index+1), None)
        if not self.reader:
            self.live_channel_names = dict(self.channel_names)
        self.update_channel_label(index)

    def update_channel_label(self, index):
        title = self.channel_title(index)
        physical = self.channel_title(index, physical=True)
        self.curves[index].opts['name'] = title
        self.legend.getLabel(self.curves[index]).setText(escape(title))
        self.legend.updateSize()
        self.update_graph_readout(index)
        self.channel_baselines[index].setToolTip(f'{physical}: drag the triangle to move this trace. The label shows measured voltage, without the display offset. Live: newest sample; hold/replay: last visible sample.')
        note = 'Replay names apply to this view and CSV exports; the source recording is unchanged.' if self.reader else 'Names are included in recordings; new launches start unnamed. Leave blank to show the channel number.'
        self.name_controls[index].setToolTip(f'{physical}\n{note}')
        self.channel_checks[index].setToolTip(f'{physical}: show/hide this trace. Acquisition and recording continue unchanged.')

    def update_graph_readout(self, index):
        title = self.channel_title(index).replace('{', '{{').replace('}', '}}')
        text = f'{title} · {self.channel_values[index].text()}'
        label = self.channel_baselines[index].label
        if label.format != text:
            label.setFormat(text)

    def commit_channel_names(self):
        for i, control in enumerate(self.name_controls):
            control.blockSignals(True)
            control.setText(self.channel_names.get(str(i+1), ''))
            control.setCursorPosition(0)
            control.blockSignals(False)
        if self.reader:
            return
        self.live_channel_names = dict(self.channel_names)
        if self.active():
            self.worker.update_channel_names(self.channel_names)

    def update_channel_cards(self):
        self.video_action.setEnabled(self.reader is not None and not self.active())
        available = set(self.recorded_channels()) if self.reader else set(range(1, 9))
        for i, check in enumerate(self.channel_checks):
            enabled = check.isChecked() and i+1 in available
            check.setEnabled(i+1 in available)
            check.setText('ON' if enabled else 'OFF')
            check.setToolTip(f'{self.channel_title(i, physical=True)}: show/hide this trace. Acquisition and recording continue unchanged.')
            self.name_controls[i].setEnabled(i+1 in available)
            self.curves[i].setVisible(enabled)
            self.channel_baselines[i].setVisible(enabled)
            self.position_controls[i].setEnabled(enabled)
            self.probes[i].setEnabled(enabled and not self.recording_active and not self.recording_pending)
            if not enabled:
                self.curves[i].clear()
                self.readout_values.pop(i, None)
                self.readout_ranges.pop(i, None)
                self.channel_values[i].setText("OFF")
                self.channel_stats[i].setText("not recorded" if i+1 not in available else "hidden · still captured" if not self.reader else "hidden")
            elif self.channel_values[i].text() == "OFF":
                self.channel_values[i].setText("— V")
                self.channel_stats[i].setText("min —   max —")
            self.update_graph_readout(i)
        if not self.active() and not self.restart_pending:
            self.run_button.setEnabled(True)
        self.channel_note.setText("Replay: switches hide traces. Drag colored zero markers to move them; voltage readings stay unchanged." if self.reader else "All 8 inputs stay captured. Switches only hide traces. Drag colored zero markers to move them; voltage readings stay unchanged.")
        self.update_acquisition_note()
        self.update_cursors()
        self.reference_overlay.render()

    def request_configuration(self):
        self.update_acquisition_note()
        if self.active() or self.restart_pending:
            self.restart_pending = True
            self.resume_recording = self.resume_recording or self.recording_requested
            self.reconfigure_timer.start()
            self.badge.setText("CHANGING TIME SCALE")

    def apply_configuration(self):
        if self.active():
            mode, channels, ns = self.capture_configuration()
            if channels == self.worker.channels and mode == self.worker.mode and (mode == "roll" or ns == self.worker.ns_per_div) and not self.worker.stop_event.is_set():
                self.restart_pending = self.resume_recording = False
                return
            self.run_button.setEnabled(False)
            self.record_button.setEnabled(False)
            self.worker.stop()
            self.message.setText("Saving the current recording and applying the acquisition time scale…")
        elif self.restart_pending and not self.closing:
            self.restart_pending = False
            self.start_acquisition()

    def probes_changed(self, *_):
        self.draw(*self.displayed)

    def active(self):
        return self.worker is not None and self.worker.isRunning()

    def toggle_run(self):
        if self.active():
            self.stop_acquisition()
        else:
            self.start_acquisition()

    def usb_setup(self):
        if self.active():
            return False
        configured = show_usb_setup(self, self.detection)
        self.message.setText("USB access is configured. Connect + run to begin." if configured else "USB setup postponed. Choose USB setup when ready.")
        if configured:
            self.check_device()
        return configured

    def apply_detection(self, result):
        self.detection = result
        self.device_label.setText(result.title)
        self.device_label.setToolTip(result.detail)
        self.device_label.setStyleSheet('color: #77ddac' if result.ready else 'color: #f5c65a')
        if not self.active() and not self.reader:
            self.badge.setText('READY' if result.ready else 'NO DEVICE' if result.state == 'missing' else 'CHECK USB')
            self.message.setText(result.detail)

    def check_device(self):
        if self.active():
            return False
        self.detect_button.setEnabled(False)
        try:
            self.apply_detection(scan_device(self))
            return self.detection.ready
        finally:
            self.detect_button.setEnabled(True)

    def start_acquisition(self):
        if self.worker is not None or self.closing:
            return
        if not usb_configured() and not self.usb_setup():
            return
        if not self.check_device():
            return
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.restore_live_visibility()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1 if self.time_window.currentData() < 1 else 0)
        self.mode_combo.blockSignals(False)
        self.playback_slider.hide()
        mode, channels, ns = self.capture_configuration()
        self.configure_time_controls(mode)
        rate = nominal_rate(mode, channels, ns)
        self.buffer = FrameBuffer(len(channels)) if mode == "burst" else SampleBuffer(int(np.ceil(rate*HISTORY_SECONDS)), len(channels))
        self.metadata = None
        self.last_error = None
        self.message.setStyleSheet("")
        self.first_data = True
        self.last_readout_key = None
        self.readout_values.clear()
        self.readout_ranges.clear()
        self.last_total = -1
        self.displayed = (0, np.empty((0, len(channels)), dtype="<u2"))
        self.view_samples = self.displayed
        self.held = False
        self.hold_button.setChecked(False)
        self.hold_button.setEnabled(True)
        self.hold_button.setText("Hold display")
        for curve in self.curves:
            curve.clear()
        for value in self.channel_values:
            value.setText("— V")
        for stats in self.channel_stats:
            stats.setText("min —   max —")
        self.worker = Acquisition(self.buffer, self, mode=mode, channels=channels, ns_per_div=ns)
        self.worker.status.connect(self.worker_status)
        self.worker.ready.connect(self.device_ready)
        self.worker.failed.connect(self.on_error)
        self.worker.recording.connect(self.on_recording)
        self.worker.finished.connect(self.acquisition_finished)
        self.run_button.setText("■  Stop")
        self.badge.setText("CONNECTING")
        self.open_button.setEnabled(False)
        self.usb_setup_button.setEnabled(False)
        self.detect_button.setEnabled(False)
        self.run_button.setEnabled(True)
        self.recording_requested = False
        self.update_channel_cards()
        self.worker.start()

    def stop_acquisition(self):
        self.reconfigure_timer.stop()
        self.restart_pending = self.resume_recording = self.recording_requested = False
        if self.active():
            self.run_button.setEnabled(False)
            self.record_button.setEnabled(False)
            self.worker.stop()
            self.badge.setText("STOPPING")

    def worker_status(self, message):
        if not self.last_error:
            self.message.setText(message)

    def device_ready(self, metadata):
        self.metadata = metadata
        self.update_acquisition_note()
        if self.worker.stop_event.is_set():
            return
        self.badge.setText("LIVE")
        self.device_label.setText("Hantek connected · acquiring samples")
        self.record_button.setEnabled(True)
        if self.resume_recording:
            self.resume_recording = False
            self.resume_named_recording()

    def on_error(self, message):
        self.last_error = message
        self.resume_recording = self.restart_pending = self.recording_requested = False
        self.message.setText("Error · " + message)
        self.message.setStyleSheet("color: #ffb0a2")
        if self.active():
            self.device_label.setText("Acquisition error · stop and Detect device to check USB")
            self.device_label.setStyleSheet('color: #ffb0a2')

    def acquisition_finished(self):
        if self.metadata and not self.held:
            self.displayed = self.buffer.snapshot(int(self.time_window.currentData() * self.sample_rate()))
            self.draw(*self.displayed)
        self.run_button.setText("▶  Connect + run")
        self.run_button.setEnabled(True)
        self.record_button.setEnabled(False)
        self.recording_active = self.recording_pending = False
        self.record_button.setText("●  Record")
        self.open_button.setEnabled(True)
        self.usb_setup_button.setEnabled(True)
        self.detect_button.setEnabled(True)
        self.device_label.setText("Acquisition stopped · Detect device to refresh USB status")
        self.mode_combo.setEnabled(True)
        self.badge.setText("ERROR" if self.last_error else "STOPPED")
        if not self.last_error:
            self.message.setText("Stopped. The last trace is retained; reconnect for a new acquisition.")
        self.worker.deleteLater()
        self.worker = None
        self.recording_requested = False
        self.update_channel_cards()
        if self.closing:
            self.close()
        elif self.restart_pending and not self.last_error:
            self.reconfigure_timer.start(1)

    def toggle_record(self):
        if not self.can_record():
            return
        if self.recording_active:
            self.recording_pending = True
            self.recording_requested = False
            self.record_button.setEnabled(False)
            self.worker.stop_recording()
            self.update_channel_cards()
            return
        worker = self.worker
        path = self.prompt_recording_path()
        if path is None:
            return
        if self.worker is not worker or not self.can_record():
            self.message.setText("Acquisition changed while naming the recording. Connect + run, then press Record again.")
            return
        self.recording_name = path.stem
        self.recording_part = 1
        self.begin_recording(path)

    def can_record(self):
        return (self.active() and self.metadata is not None and not self.recording_pending
                and not self.worker.stop_event.is_set() and not self.restart_pending and not self.closing)

    def prompt_recording_path(self):
        dialog = W.QDialog(self)
        dialog.setWindowTitle("Name recording · TraceForge")
        dialog.setMinimumWidth(600)
        layout = W.QVBoxLayout(dialog)
        layout.addWidget(W.QLabel("Name this recording. It will save automatically while recording."))
        name = W.QLineEdit()
        name.setObjectName("recordingName")
        name.setPlaceholderText("Battery check")
        layout.addWidget(name)
        destination = W.QLabel()
        destination.setObjectName("recordingDestination")
        destination.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        destination.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(destination)
        error = W.QLabel()
        error.setObjectName("recordingNameError")
        error.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        error.setStyleSheet("color: #ffb0a2")
        error.setWordWrap(True)
        layout.addWidget(error)
        controls = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Save | W.QDialogButtonBox.StandardButton.Cancel)
        start = controls.button(W.QDialogButtonBox.StandardButton.Save)
        start.setText("Start recording")
        selected = None
        def validate():
            try:
                path = new_recording_path(name.text())
            except (OSError, ValueError) as exc:
                destination.setText(f"Folder: {recordings_directory()} / {dt.date.today().isoformat()}")
                error.setText(str(exc) if name.text() else "")
                start.setEnabled(False)
                return None
            destination.setText(f"Save to: {path}")
            error.clear()
            start.setEnabled(True)
            return path
        def accept():
            nonlocal selected
            selected = validate()
            if selected is not None:
                dialog.accept()
        name.textChanged.connect(validate)
        controls.accepted.connect(accept)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        validate()
        name.setFocus()
        return selected if dialog.exec() == W.QDialog.DialogCode.Accepted else None

    def begin_recording(self, path):
        self.last_recording = path
        self.recording_pending = True
        self.recording_requested = True
        self.record_button.setEnabled(False)
        self.worker.start_recording(str(path), self.probe_factors(), dict(self.channel_names))
        self.update_channel_cards()

    def resume_named_recording(self):
        if not self.can_record() or self.recording_name is None:
            return
        for part in range(self.recording_part + 1, self.recording_part + 1001):
            try:
                path = new_recording_path(self.recording_name, part=part)
            except FileExistsError:
                continue
            except (OSError, ValueError) as exc:
                self.message.setText(f"Recording could not resume: {exc}")
                return
            self.recording_part = part
            self.begin_recording(path)
            return
        self.message.setText("Recording could not resume: no unused part filename. Press Record to choose a new name.")

    def update_recording_location(self):
        folder = recordings_directory()
        self.recording_location_label.setText(f"Save to: {folder} / date / your-name.hscope")
        self.recording_location_label.setToolTip(str(folder))

    def choose_recording_directory(self):
        dialog = W.QDialog(self)
        dialog.setWindowTitle("Recording folder · TraceForge")
        dialog.setMinimumWidth(600)
        layout = W.QVBoxLayout(dialog)
        explanation = W.QLabel("Choose the folder for future recordings. Press Record to name each file; recordings are grouped by date.\n\n"
                               "Existing recordings stay where they are. An active recording keeps its current file; the next recording uses this choice.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        location = W.QLineEdit(str(recordings_directory()))
        location.setObjectName("recordingFolderPath")
        layout.addWidget(location)
        tools = W.QHBoxLayout()
        def browse():
            selected = W.QFileDialog.getExistingDirectory(dialog, "Choose recording folder", location.text())
            if selected:
                location.setText(selected)
        tools.addWidget(button("Browse…", browse))
        tools.addWidget(button("Use default", lambda: location.setText(str(default_recordings_directory()))))
        tools.addStretch()
        layout.addLayout(tools)
        example = W.QLabel("Within the chosen folder: YYYY-MM-DD / your-name.hscope")
        example.setWordWrap(True)
        layout.addWidget(example)
        controls = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Save | W.QDialogButtonBox.StandardButton.Cancel)
        def save():
            try:
                set_recordings_directory(location.text())
            except (OSError, ValueError) as exc:
                W.QMessageBox.warning(dialog, "Cannot use this folder", str(exc))
                return
            self.update_recording_location()
            self.message.setText(f"Future recordings will be saved under {recordings_directory()}.")
            dialog.accept()
        controls.accepted.connect(save)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        dialog.exec()

    def on_recording(self, enabled, path):
        self.recording_active = enabled
        self.recording_pending = False
        self.record_button.setEnabled(self.active() and not self.worker.stop_event.is_set())
        self.record_button.setText("■  Finish recording" if enabled else "●  Record")
        self.update_channel_cards()
        if path:
            self.last_recording = Path(path)
            self.message.setText(("Recording all 8 inputs → " if enabled else "Saved → ") + path)
        if not self.held:
            self.badge.setText("REC • LIVE" if enabled else "LIVE")

    def toggle_hold(self, checked):
        self.held = checked
        self.hold_button.setText("Resume display" if checked else "Hold display")
        if self.active():
            self.badge.setText("REC • HOLD" if checked and self.recording_active else "HOLD" if checked else "REC • LIVE" if self.recording_active else "LIVE")
        if not checked:
            self.last_total = -1
            self.last_readout_key = None
            self.update_latest_readouts()
        elif self.metadata:
            self.draw(*self.displayed)

    def window_changed(self, *_):
        self.last_total = -1
        self.update_time_axis()
        if self.reader:
            self.render_playback()
            return
        mode, channels, ns = self.capture_configuration()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1 if mode == "burst" else 0)
        self.mode_combo.blockSignals(False)
        self.request_configuration()
        if self.metadata:
            if not self.active() and not self.burst_view():
                self.displayed = self.buffer.snapshot(int(self.time_window.currentData() * self.sample_rate()))
            self.draw(*self.displayed)

    def set_voltage_span(self, *_):
        span = self.voltage_span.currentData()
        if span is None:
            self.fit_voltage()
        elif span == "manual":
            return
        else:
            lower, upper = self.plot.viewRange()[1]
            center = (lower + upper) / 2
            self.plot.setYRange(center-span/2, center+span/2, padding=0)

    def set_channel_offset(self, channel, offset):
        self.auto_stack_pending = False
        self.apply_trace_positions({channel: offset})

    def apply_trace_positions(self, positions):
        for channel, offset in positions.items():
            value = round(float(np.clip(offset, -100000, 100000)), 3)
            self.channel_offsets[channel] = value
            for control in (self.position_controls[channel], self.channel_baselines[channel]):
                control.blockSignals(True)
                control.setValue(value)
                control.blockSignals(False)
        self.voltage_span.blockSignals(True)
        self.voltage_span.setCurrentIndex(self.voltage_span.findData("manual"))
        self.voltage_span.blockSignals(False)
        self.plot.setLabel("left", "Voltage + display offset" if np.any(self.channel_offsets) else "Voltage", units="V")
        self.draw(*self.displayed)
        self.update_cursors()
        self.reference_overlay.render()

    def stack_traces(self, *_, automatic=False):
        visible = [ch-1 for ch in self.recorded_channels() if self.channel_checks[ch-1].isChecked()]
        if not visible:
            self.auto_stack_pending = False
            if not automatic:
                self.message.setText("Show at least one channel, then choose Stack traces.")
            return
        bounds = {ch: (0., 1.) for ch in visible}
        raw = self.view_samples[1] if len(self.view_samples[1]) else self.displayed[1]
        self.auto_stack_pending = self.metadata is None or not len(raw)
        if self.metadata is not None and len(raw):
            volts = to_volts(raw, self.metadata, self.probe_factors())
            for column, ch in enumerate(self.recorded_channels()):
                if ch-1 in bounds:
                    bounds[ch-1] = (min(0., float(np.min(volts[:, column]))), max(0., float(np.max(volts[:, column]))))
        spacing = max(1., max(high-low for low, high in bounds.values())) * 1.35
        positions = {ch: ((len(visible)-1)/2-rank)*spacing - sum(bounds[ch])/2
                     for rank, ch in enumerate(visible)}
        self.apply_trace_positions(positions)
        if self.metadata is not None and len(raw):
            self.fit_voltage()
        else:
            self.plot.setYRange(-len(visible)*spacing/2, len(visible)*spacing/2, padding=.1)

    def reset_trace_positions(self):
        self.auto_stack_pending = False
        self.apply_trace_positions(dict.fromkeys(range(8), 0.))
        self.fit_voltage()

    def fit_voltage(self, *_, only_if_needed=False):
        if self.metadata is None or not len(self.displayed[1]):
            return
        visible = [column for column, ch in enumerate(self.recorded_channels()) if self.channel_checks[ch-1].isChecked()]
        if not visible:
            return
        raw = self.view_samples[1] if len(self.view_samples[1]) else self.displayed[1]
        channels = self.recorded_channels()
        offsets = self.channel_offsets[np.asarray(channels)-1][visible]
        volts = self.graph_voltages(to_volts(raw, self.metadata, self.probe_factors()))[:, visible] + offsets
        lower, upper = float(np.min(volts)), float(np.max(volts))
        if np.any(offsets):
            lower, upper = min(lower, float(np.min(offsets))), max(upper, float(np.max(offsets)))
        pad = max(0.08, (upper-lower)*0.15)
        if only_if_needed:
            old_lower, old_upper = self.plot.viewRange()[1]
            needed_span = upper-lower+2*pad
            
            if lower >= old_lower+pad/3 and upper <= old_upper-pad/3 and old_upper-old_lower < needed_span*3:
                return
        self.plot.setYRange(lower-pad, upper+pad, padding=0)

    def update_latest_readouts(self):
        if not self.active() or self.metadata is None or self.held or self.reader:
            return
        index, raw = self.buffer.latest()
        factors = self.probe_factors()
        key = (index, tuple(factors), tuple(check.isChecked() for check in self.channel_checks))
        if not len(raw) or key == self.last_readout_key:
            return
        self.last_readout_key = key
        self.set_voltage_readouts(to_volts(raw, self.metadata, factors)[-1])

    def format_readout(self, value):
        return f'{float(value):z.{READOUT_FILTERS[self.noise_filter_index][1]}f}'

    def cycle_noise_filter(self):
        self.noise_filter_index = (self.noise_filter_index + 1) % len(READOUT_FILTERS)
        self.noise_filter_button.setText(f'Noise filter: {READOUT_FILTERS[self.noise_filter_index][0]}')
        for index in range(8):
            self.render_readout(index)
        if self.graph_filter_button.isChecked():
            self.redraw_graph_filter()

    def toggle_graph_filter(self, checked):
        self.graph_filter_button.setText(f'Apply filter to graph: {"ON" if checked else "OFF"}')
        self.redraw_graph_filter()

    def graph_voltages(self, volts):
        if self.graph_filter_button.isChecked() and self.noise_filter_index:
            return np.round(volts, READOUT_FILTERS[self.noise_filter_index][1])
        return volts

    def redraw_graph_filter(self):
        raw = self.view_samples[1]
        if self.metadata is None or not len(raw):
            return
        volts = self.graph_voltages(to_volts(raw, self.metadata, self.probe_factors()))
        for column, ch in enumerate(self.recorded_channels()):
            curve = self.curves[ch-1]
            x, _ = curve.getOriginalDataset()
            if self.channel_checks[ch-1].isChecked() and x is not None and len(x) == len(raw):
                curve.setData(x, volts[:, column] + self.channel_offsets[ch-1])

    def render_readout(self, index):
        if index in self.readout_values:
            self.channel_values[index].setText(f'{self.format_readout(self.readout_values[index])} V')
        if index in self.readout_ranges:
            lower, upper = self.readout_ranges[index]
            self.channel_stats[index].setText(f'min {self.format_readout(lower)}   max {self.format_readout(upper)}')
        self.update_graph_readout(index)

    def set_voltage_readouts(self, values):
        for column, ch in enumerate(self.recorded_channels()):
            if self.channel_checks[ch-1].isChecked():
                self.readout_values[ch-1] = float(values[column])
                self.render_readout(ch-1)

    def refresh(self):
        if not self.active() or self.metadata is None:
            return
        total = self.buffer.total
        if not self.held and total != self.last_total:
            self.last_total = total
            self.displayed = self.buffer.snapshot(int(self.time_window.currentData() * self.sample_rate()))
            self.draw(*self.displayed)
            if self.voltage_span.currentData() is None and len(self.displayed[1]):
                self.fit_voltage(only_if_needed=not self.first_data)
            self.first_data = False
        age = time.monotonic() - self.worker.last_received if self.worker.last_received else 0
        if self.restart_pending or self.worker.stop_event.is_set():
            return
        if age > 2:
            self.badge.setText("NO NEW DATA")
        elif not self.held:
            self.badge.setText("REC • LIVE" if self.recording_active else "LIVE")
        if self.burst_view():
            self.statusBar().showMessage(f"{self.buffer.frame_count:,} independent frames   ·   {len(self.displayed[1])} samples/ch in frame   ·   {format_time(1/self.sample_rate())}/sample nominal   ·   {len(self.recorded_channels())} inputs   ·   clock validation pending")
        else:
            self.statusBar().showMessage(f"{total:,} received samples / channel   ·   {format_time(total/self.sample_rate())} nominal   ·   {self.sample_rate():.5g} Sa/s/ch   ·   {len(self.recorded_channels())} inputs   ·   timebase validation pending")

    def draw(self, start, raw):
        if self.metadata is None or not len(raw):
            return
        rate = float(self.metadata["sample_rate_nominal_hz"])
        span = float(self.time_window.currentData())
        fast = self.burst_view()
        if fast:
            frame_span = (len(raw)-1)/rate
            left = max(0, frame_span-span) * self.frame_position.value()/10000
            begin = max(0, int(np.ceil(left*rate-1e-9)))
            end = min(len(raw), int(np.floor((left+span)*rate+1e-9))+1)
            x = np.arange(begin, end)/rate
            raw = raw[begin:end]
            self.view_samples = (start+begin, raw.copy())
        else:
            x = (start + np.arange(len(raw))) / rate
            left = max(0, x[-1]-span) if self.reader is None else x[0]
            self.view_samples = (start, raw.copy())
        if not len(raw):
            return
        if self.auto_stack_pending:
            self.stack_traces(automatic=True)
            return
        volts = to_volts(raw, self.metadata, self.probe_factors())
        graph_volts = self.graph_voltages(volts)
        for column, ch in enumerate(self.recorded_channels()):
            i = ch-1
            if not self.channel_checks[i].isChecked():
                continue
            dots = fast and len(raw) <= 50
            self.curves[i].setData(x, graph_volts[:, column] + self.channel_offsets[i], symbol="o" if dots else None,
                                   symbolSize=5, symbolBrush=COLORS[i], symbolPen=None)
            self.readout_ranges[i] = (float(np.min(volts[:, column])), float(np.max(volts[:, column])))
            self.render_readout(i)
        if self.active() and not self.held and self.reader is None:
            self.update_latest_readouts()
        else:
            self.set_voltage_readouts(volts[-1])
        self.plot.setXRange(left, left+span, padding=0)
        self.reference_overlay.render()
        if self.voltage_span.currentData() is None and (self.reader is not None or self.held):
            self.fit_voltage(only_if_needed=True)

    def toggle_cursors(self, checked):
        if checked:
            (x0, x1), (y0, y1) = self.plot.viewRange()
            for cursor, position in zip(self.cursors, [x0+.3*(x1-x0), x0+.7*(x1-x0), y0+.3*(y1-y0), y0+.7*(y1-y0)]):
                cursor.setValue(position)
            if self.active() and not self.held:
                self.hold_button.setChecked(True)
                self.toggle_hold(True)
        for cursor in self.cursors:
            cursor.setVisible(checked)
        self.update_cursors()

    def update_cursors(self, *_):
        shifted = any(self.channel_offsets[ch-1] != 0 and self.channel_checks[ch-1].isChecked() for ch in self.recorded_channels())
        if not self.cursor_button.isChecked():
            self.cursor_readout.setText("Traces are vertically shifted. Channel readouts show measured voltage; ΔY cursors include display offsets." if shifted else "Cursors measure Δt, 1/Δt and ΔV. The timebase remains nominal.")
            return
        elapsed = abs(self.cursors[1].value()-self.cursors[0].value())
        voltage = abs(self.cursors[3].value()-self.cursors[2].value())
        frequency = f"{1/elapsed:.4g} Hz" if elapsed > 1e-12 else "—"
        spacing = 1/self.metadata["sample_rate_nominal_hz"] if self.metadata else None
        note = " · below sample spacing" if spacing and 0 < elapsed < spacing else ""
        label = "ΔY (display)" if shifted else "ΔV"
        self.cursor_readout.setText(f"Δt  {format_time(elapsed)} nominal     ·     1/Δt  {frequency}     ·     {label}  {voltage:.5g} V{note}")

    def open_dialog(self):
        path, _ = W.QFileDialog.getOpenFileName(self, "Open Hantek recording", str(recordings_directory()), "Hantek recordings (*.hscope)")
        if path:
            self.open_capture(path)

    def open_capture(self, path):
        if self.active():
            return
        try:
            reader = CaptureReader(path)
            if not reader.count:
                reader.close()
                raise ValueError("This recording contains no committed samples")
            if self.reader:
                self.reader.close()
            self.reader = reader
            self.metadata = reader.metadata
            self.apply_channel_names(self.metadata.get('channel_names', {}))
            self.auto_stack_pending = True
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(1 if reader.mode == "burst" else 0)
            self.mode_combo.blockSignals(False)
            self.configure_time_controls(reader.mode)
            factors = self.metadata.get("probe_factors_at_record_start", [1]*len(reader.channel_order))
            for ch, factor in zip(reader.channel_order, factors):
                combo = self.probes[ch-1]
                combo.blockSignals(True)
                combo.setCurrentText(f"{factor}×")
                combo.blockSignals(False)
            for i, check in enumerate(self.channel_checks):
                check.blockSignals(True)
                check.setChecked(i+1 in reader.channel_order)
                check.blockSignals(False)
            self.update_channel_cards()
            self.held = True
            self.hold_button.setChecked(True)
            self.hold_button.setText("Recorded view")
            self.hold_button.setEnabled(False)
            self.playback_slider.show()
            self.playback_slider.setRange(0, len(reader.frames)-1 if reader.mode == "burst" else 10000)
            self.playback_slider.setValue(0)
            self.playback_label.setText("Recorded frame" if reader.mode == "burst" else "Recording position")
            self.badge.setText("REPLAY")
            self.message.setStyleSheet("")
            self.message.setText(f"{Path(path).name} · {reader.count:,} samples / channel · " + ("Finalized recording" if self.metadata.get("complete") else "Interrupted recording · showing recoverable committed data"))
            self.statusBar().showMessage(f"{len(reader.frames)} separate frames · gaps not plotted · {format_time(1/self.sample_rate())}/sample nominal" if reader.mode == "burst" else f"Recording length: {format_time(reader.count / self.sample_rate())} nominal · raw samples preserved · timebase validation pending")
            self.render_playback()
            self.fit_voltage()
            self.tabs.setCurrentIndex(0)
        except Exception as exc:
            self.on_error(f"Cannot open recording: {exc}")

    def render_playback(self):
        if self.reader is None:
            return
        if self.reader.mode == "burst":
            self.displayed = self.reader.frame(self.playback_slider.value())
            self.draw(*self.displayed)
            return
        n = int(self.time_window.currentData() * self.metadata["sample_rate_nominal_hz"])
        offset = round(max(0, self.reader.count - n) * self.playback_slider.value()/10000)
        start = self.reader.start + offset
        self.displayed = self.reader.window(start, start+n)
        self.draw(*self.displayed)

    def export_view_dialog(self):
        if self.metadata is None or not len(self.displayed[1]):
            self.message.setText("No samples to export yet.")
            return
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        folder = self.reader.path.parent if self.reader else self.last_recording.parent if self.last_recording else recordings_directory()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            W.QMessageBox.warning(self, "Export folder unavailable", str(exc))
            return
        path, _ = W.QFileDialog.getSaveFileName(self, "Export displayed raw and voltage samples", str(folder / f"view-{stamp}.csv"), "CSV (*.csv)")
        if path:
            try:
                self.export_view(path)
                self.message.setText(f"Exported displayed window and acquisition metadata → {path}")
            except Exception as exc:
                self.on_error(f"Export: {exc}")

    def export_video_dialog(self):
        if self.reader is not None and not self.active():
            VideoDialog(self).exec()

    def export_view(self, path):
        path = Path(path)
        start, raw = self.view_samples
        if self.metadata is None or not len(raw):
            raise ValueError("No samples available")
        sidecar = path.with_suffix(path.suffix + ".metadata.json")
        
        
        if sidecar.exists():
            raise FileExistsError(f"Metadata already exists: {sidecar.name}. Choose a new filename.")
        volts = to_volts(raw, self.metadata, self.probe_factors())
        rate = self.metadata["sample_rate_nominal_hz"]
        with path.open("w", newline="") as file:
            writer = csv.writer(file)
            fast = self.burst_view()
            columns = ["sample_index", "sample_in_frame", "time_in_frame_nominal_s", "time_in_frame_nominal_us"] if fast else ["sample_index", "time_nominal_s"]
            writer.writerow(columns + [f"CH{ch}_raw" for ch in self.recorded_channels()] + [f"CH{ch}_V" for ch in self.recorded_channels()])
            for offset, (counts, voltage) in enumerate(zip(raw, volts)):
                index = start+offset
                within = index-self.displayed[0]
                timing = [index, within, f"{within/rate:.12f}", f"{within/rate*1e6:.9f}"] if fast else [index, f"{index/rate:.9f}"]
                writer.writerow(timing + counts.tolist() + [f"{v:.9f}" for v in voltage])
        meta = dict(self.metadata, exported_start_index=start, exported_samples_per_channel=len(raw),
                    channel_names={str(ch): self.channel_names[str(ch)] for ch in self.recorded_channels() if str(ch) in self.channel_names},
                    export_probe_factors=self.probe_factors(),
                    display_offsets_v={f"CH{ch}": float(self.channel_offsets[ch-1]) for ch in self.recorded_channels()},
                    display_noise_filter=READOUT_FILTERS[self.noise_filter_index][0],
                    display_graph_filter=self.graph_filter_button.isChecked(),
                    exported_voltage_includes_graph_filter=False,
                    exported_voltage_includes_display_offsets=False)
        if self.burst_view():
            meta["exported_frame_start_index"] = self.displayed[0]
        sidecar.write_text(json.dumps(meta, indent=2) + "\n")

    def about(self):
        W.QMessageBox.information(self, "About TraceForge",
            f"TraceForge {VERSION}\nfor Linux · Hantek 1008 series\nTested hardware: Hantek 1008B\n\n"
            "Native Python + PySide6 + PyQtGraph interface with direct PyUSB acquisition.\n\n"
            "Time/div covers 1 µs to 1 min. Fast frames cover µs/ms; rolling capture covers seconds/minutes. "
            "All eight inputs stay captured. Channel switches only hide/show traces, without restarting acquisition or splitting recordings. "
            "The fastest nominal rate with eight inputs is 300 kSa/s/ch (3.333333 µs/sample). "
            "Drag colored zero markers or use Position to move individual traces; Stack traces separates visible signals. "
            "Positions do not change measured voltage, raw recordings, or CSV voltage samples. "
            "Each fast capture is a separate frame; transfer gaps are not joined together. "
            "Voltage uses startup internal-zero measurements and a nominal 0.01 V/count scale. "
            "A 1×/10×/20×/100× selector only scales the displayed voltage; match your physical probe.\n\n"
            "The two AA checks validate CH1 near 1.3–1.6 V only. A known-frequency input is still needed "
            "to validate the timebase. The fast timing comes from the vendor clock calculation "
            "and has not yet been checked against an independent signal. Smaller display divisions "
            "do not increase the actual sampling rate.\n\n"
            "Record saves all received raw channels and conversion metadata in a .hscope SQLite file. "
            "Use Open recording for review, or Export view for a CSV and metadata JSON.\n\n"
            "USB driver adapted from mfg92/hantek1008py (Apache 2.0). "
            "References recovered from your local Hantek Windows installer; original vendor rights apply.")

    def closeEvent(self, event):
        self.commit_channel_names()
        if self.active():
            self.closing = True
            self.stop_acquisition()
            self.message.setText("Finishing recording and releasing the scope…")
            event.ignore()
        else:
            if self.reader:
                self.reader.close()
                self.reader = None
            self.reconfigure_timer.stop()
            self.library.arrays.close()
            event.accept()


def prepare_startup(force_usb=False, force_shortcuts=False):
    """Always detect first, including when both setup choices were remembered."""
    result = scan_device()
    if force_usb or not setup_complete():
        if not show_usb_setup(detection=result):
            return None
        result = scan_device()
    ensure_shortcuts(force=force_shortcuts)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"TraceForge {VERSION} for Linux")
    parser.add_argument("--connect", action="store_true", help="Connect to the real scope at startup")
    parser.add_argument("--mode", choices=["roll", "burst"], default="roll", help="Rolling seconds or fast microsecond frames")
    parser.add_argument("--open", dest="capture", type=Path, help="Open a saved .hscope recording")
    parser.add_argument("--channels", help="Initially visible traces, such as 1 or 1,3; all eight inputs are captured")
    parser.add_argument("--time-div", type=float, help="Initial seconds per division, such as 0.00005 or 0.5")
    parser.add_argument("--usb-setup", action="store_true", help="Show the USB permission setup dialog")
    parser.add_argument("--shortcuts", action="store_true", help="Choose desktop and application-menu shortcuts")
    parser.add_argument("--self-test", type=Path, metavar="REPORT", help="Check packaged UI, USB backend, recording and replay; write JSON")
    parser.add_argument("--test-usb", action="store_true", help="With --self-test, also capture from the connected scope")
    args = parser.parse_args()
    if args.test_usb and not args.self_test:
        parser.error("--test-usb requires --self-test")
    selected = None
    if args.channels:
        try:
            selected = sorted(int(value)-1 for value in args.channels.split(","))
            if not selected or len(set(selected)) != len(selected) or any(ch not in range(8) for ch in selected):
                raise ValueError()
        except ValueError:
            parser.error("--channels must list unique inputs from 1 to 8")
    division_index = None
    if args.time_div is not None:
        division_index = next((i for i, value in enumerate(TIME_DIVISIONS) if np.isclose(value, args.time_div, rtol=1e-10, atol=1e-14)), None)
        if division_index is None:
            parser.error("Unsupported --time-div; use a value available in the Time / division selector")
    if args.self_test:
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = W.QApplication(sys.argv)
    app.setApplicationName("TraceForge")
    app.setDesktopFileName("hantek-linux")
    app.setWindowIcon(QtGui.QIcon(str(ROOT / "icon.svg")))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    pg.setConfigOptions(antialias=False, useOpenGL=False)
    
    initial_detection = None
    if not args.self_test:
        app.setQuitOnLastWindowClosed(False)
        try:
            initial_detection = prepare_startup(args.usb_setup, args.shortcuts)
            if initial_detection is None:
                return 0
        except OSError as exc:
            W.QMessageBox.critical(None, "Setup could not be saved", str(exc))
            return 1
    window = MainWindow()
    if initial_detection is not None:
        window.apply_detection(initial_detection)
    window.mode_combo.setCurrentIndex(1 if args.mode == "burst" else 0)
    if selected is not None:
        window.select_channels(selected)
    if division_index is not None:
        window.time_window.setCurrentIndex(division_index)
    signal.signal(signal.SIGTERM, lambda *_: window.close())
    signal.signal(signal.SIGINT, lambda *_: window.close())
    window.show()
    app.setQuitOnLastWindowClosed(True)
    if args.self_test:
        from package_check import verify_package
        return verify_package(app, window, args.self_test, args.test_usb)
    def startup():
        if args.capture:
            window.open_capture(args.capture)
        elif args.connect:
            if initial_detection is not None and initial_detection.ready:
                window.start_acquisition()
    QtCore.QTimer.singleShot(100, startup)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
