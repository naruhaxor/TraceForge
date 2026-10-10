"""USB is owned by one background worker; rendering never drives sampling."""
from __future__ import annotations

import datetime as dt
import queue
import threading
import time
import traceback

import numpy as np
import usb.util
from PySide6.QtCore import QThread, Signal

from core import RATE, BURST_NS_PER_DIV, BURST_SLOT_US, VOLTS_PER_COUNT, CaptureWriter, scan_channels, nominal_rate
from vendor.hantek1008 import Hantek1008Raw
from channel_names import normalize_names


class Acquisition(QThread):
    status = Signal(str)
    ready = Signal(dict)
    failed = Signal(str)
    recording = Signal(bool, str)

    def __init__(self, buffer, parent=None, mode="roll", channels=None, ns_per_div=BURST_NS_PER_DIV):
        super().__init__(parent)
        self.buffer = buffer
        if mode not in {"roll", "burst"}:
            raise ValueError("Unknown acquisition mode")
        self.mode = mode
        self.channels = sorted(range(8) if channels is None else channels)
        self.scan_channels = scan_channels(self.channels, mode)
        self.ns_per_div = ns_per_div
        self.stop_event = threading.Event()
        self.commands = queue.Queue()
        self.metadata = {}
        self.last_received = 0.0

    def stop(self):
        self.stop_event.set()

    def start_recording(self, path, probe_factors, channel_names=None):
        self.commands.put(("record", path, list(probe_factors), normalize_names(channel_names)))

    def update_channel_names(self, names):
        self.commands.put(('names', normalize_names(names)))

    def stop_recording(self):
        self.commands.put(("stop_record",))

    def run(self):
        scope = Hantek1008Raw(ns_per_div=self.ns_per_div if self.mode == "burst" else 500_000,
                              vertical_scale_factor=1.0, active_channels=self.scan_channels,
                              trigger_channel=self.channels[0])
        scope.cancel_requested = self.stop_event.is_set
        recorder = None
        stream = None
        error = None
        connected = False
        try:
            self.status.emit("Connecting to Hantek 1008B…")
            scope.connect()
            connected = True
            self.status.emit("Measuring internal zero offsets…")
            scope.init()
            if self.mode == "burst":
                scope.configure_burst_window(BURST_SLOT_US[self.ns_per_div])
            self.metadata = {
                "device": "Hantek 1008B", "usb_id": "0783:5725",
                "acquisition_started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "sample_rate_nominal_hz": nominal_rate(self.mode, self.channels, self.ns_per_div),
                "acquisition_mode": self.mode,
                "timebase_status": "nominal; independent frequency validation pending",
                "time_coordinate": "sample_index / sample_rate_nominal_hz",
                "channel_order": [ch+1 for ch in self.channels],
                "hardware_scan_channels": [ch+1 for ch in self.scan_channels],
                "padding_channels_discarded": [ch+1 for ch in self.scan_channels if ch not in self.channels],
                "zero_offsets_counts": [scope.get_zero_offsets()[1.0][ch] for ch in self.channels],
                "zero_offsets_all_gains": scope.get_zero_offsets(),
                "volts_per_count": [VOLTS_PER_COUNT] * len(self.channels),
                "hardware_scale_factors": [1.0] * len(self.channels),
                "timebase_source": "upstream rolling nominal rate and channel-count correction factors; independent validation pending",
                "amplitude_status": "nominal scale; CH1 checked near 1.3–1.6 V only; factory gain corrections not applied",
                "device_metadata": scope.device_metadata,
                "factory_response_blocks": {f"{k:02X}": v.hex() for k, v in scope._device_response_blocks.items()},
                "source_continuity": "received rows are contiguous in this file; hardware packet loss cannot be excluded without a device sample counter",
            }
            if self.mode == "burst":
                self.metadata.update({
                    "time_coordinate": "sample_in_frame / sample_rate_nominal_hz; frame start is not an established trigger time",
                    "frame_layout": "one database batch per independent hardware burst; gaps between frames are unknown",
                    "partial_scan_tail_policy": "only complete channel scans are retained; a six-slot scan discards the final four values of the 4000-conversion buffer",
                    "trailing_incomplete_scan_values_discarded": 4000 % len(self.scan_channels),
                    "hardware_timebase_ns_per_div": self.ns_per_div,
                    "timebase_source": "vendor HTNTLVDll.dll dsoSetHTrigPos slot period multiplied by hardware scan count; independent validation pending",
                    "channel_timing": "channels share a conversion clock; channel-to-channel skew has not been validated",
                    "source_continuity": "samples belong to independent burst frames; no continuous timeline across frames is asserted",
                })
            self.ready.emit(self.metadata)
            if self.stop_event.is_set():
                return
            self.status.emit("Live")
            stream = self.burst_frames(scope) if self.mode == "burst" else scope.request_samples_roll_mode(sampling_rate=RATE)
            last_commit = time.monotonic()
            for channels in stream:
                while not self.commands.empty():
                    command = self.commands.get_nowait()
                    try:
                        if command[0] == "record" and recorder is None:
                            meta = dict(self.metadata, probe_factors_at_record_start=command[2], channel_names=command[3])
                            recorder = CaptureWriter(command[1], meta, self.buffer.total)
                            self.recording.emit(True, str(recorder.path))
                        elif command[0] == 'names':
                            self.metadata['channel_names'] = command[1]
                            if recorder is not None:
                                recorder.set_channel_names(command[1])
                        elif command[0] == "stop_record" and recorder is not None:
                            path = str(recorder.path)
                            recorder.close()
                            recorder = None
                            self.recording.emit(False, path)
                    except Exception as exc:
                        self.failed.emit(f"Recording: {type(exc).__name__}: {exc}")
                        if recorder is not None:
                            try:
                                recorder.close(complete=False, error=str(exc))
                            except Exception:
                                pass
                            recorder = None
                        self.recording.emit(False, "")
                raw = np.column_stack([channels[ch] for ch in self.channels]).astype("<u2")
                if len(raw) == 0:
                    continue
                now_ns = time.monotonic_ns()
                self.last_received = time.monotonic()
                index = self.buffer.append(raw)
                if recorder is not None:
                    try:
                        recorder.append(index, raw, now_ns)
                        if time.monotonic() - last_commit >= 0.5:
                            recorder.flush()
                            last_commit = time.monotonic()
                    except Exception as exc:
                        self.failed.emit(f"Recording stopped: {type(exc).__name__}: {exc}")
                        try:
                            recorder.close(complete=False, error=str(exc))
                        except Exception:
                            pass
                        recorder = None
                        self.recording.emit(False, "")
                if self.stop_event.is_set():
                    break
        except Exception as exc:
            if not (isinstance(exc, InterruptedError) and self.stop_event.is_set()):
                error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                self.failed.emit(error)
        finally:
            if stream is not None:
                stream.close()
            if recorder is not None:
                path = str(recorder.path)
                try:
                    while not self.commands.empty():
                        command = self.commands.get_nowait()
                        if command[0] == 'names':
                            recorder.set_channel_names(command[1])
                    recorder.close(complete=error is None, error=error)
                except Exception as exc:
                    self.failed.emit(f"Unable to finalize recording: {exc}")
                self.recording.emit(False, path)
            self.status.emit("Releasing USB device…")
            if connected:
                try:
                    scope.close()
                except Exception as exc:
                    self.failed.emit(f"USB cleanup: {exc}")
            if scope._dev is not None:
                usb.util.dispose_resources(scope._dev)

    def burst_frames(self, scope):
        while not self.stop_event.is_set():
            frame = scope.request_samples_burst_mode()
            lengths = {len(values) for values in frame.values()}
            if set(frame) != set(self.scan_channels) or 0 in lengths:
                raise ValueError("Burst frame does not match the configured scan channels")
            if len(lengths) != 1:
                
                
                if len(self.scan_channels) != 6 or sum(map(len, frame.values())) != 4000 or lengths != {666, 667}:
                    raise ValueError("Unexpected partial channel scan in burst frame")
                frame = {ch: values[:666] for ch, values in frame.items()}
            if not all(0 <= value <= 4095 for values in frame.values() for value in values):
                raise ValueError("Burst sample outside the 12-bit ADC range")
            yield frame
