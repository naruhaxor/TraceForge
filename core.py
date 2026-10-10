"""Acquisition data and lossless, append-only Hantek capture files.

Time coordinates come from sample indices and the nominal device rate, never
from USB delivery times. Delivery timestamps are saved separately for diagnosis.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import numpy as np

RATE = 440
CHANNELS = 8
VOLTS_PER_COUNT = 0.01
FORMAT = "hantek-linux-1"




BURST_RATE = 2_400_000 / CHANNELS
BURST_NS_PER_DIV = 100_000
BURST_TIME_DIVISIONS = (1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6)


BURST_SLOT_US = {
    100_000: 0.4166666666666667, 200_000: 0.8750001093750137,
    500_000: 1.25, 1_000_000: 2.5, 2_000_000: 5.0,
    5_000_000: 13.333333333333334, 10_000_000: 27.000027000027,
    20_000_000: 55.94405594405595, 50_000_000: 125.99218848431397,
}
TIME_DIVISIONS = tuple(n * 10.0**power for power in range(-6, 2) for n in (1, 2, 5)) + (60.0,)
HISTORY_SECONDS = 600
ROLL_RATE_FACTORS = (4.56, 3.03, 2.27, 1.82, 1.51, 1.3, 1.14, 1.0)


def channel_order(metadata):
    order = metadata.get("channel_order", list(range(1, 9)))
    if not isinstance(order, list) or not order or any(type(ch) is not int or ch not in range(1, 9) for ch in order) or len(set(order)) != len(order):
        raise ValueError("Invalid channel order")
    return order


def scan_channels(channels, mode):
    """Zero-based inputs; vendor fast mode pads 3/5/7 to an even scan count."""
    selected = sorted(channels)
    channel_order({"channel_order": [ch+1 for ch in selected]})
    if mode == "burst" and len(selected) in (3, 5, 7):
        selected.append(next(ch for ch in range(8) if ch not in selected))
    return sorted(selected)


def nominal_rate(mode, channels, ns_per_div=BURST_NS_PER_DIV):
    count = len(scan_channels(channels, mode))
    if mode == "burst":
        return 1e6 / (BURST_SLOT_US[ns_per_div] * count)
    return RATE * ROLL_RATE_FACTORS[count-1]


def format_time(seconds: float) -> str:
    magnitude = abs(seconds)
    scale, unit = (1/60, "min") if magnitude >= 60 else (1, "s") if magnitude >= 1 else (1e3, "ms") if magnitude >= 1e-3 else (1e6, "µs")
    return f"{seconds*scale:.6g} {unit}"


class SampleBuffer:
    """Bounded ring containing every received sample, in acquisition order."""

    def __init__(self, capacity: int = RATE * 120, channel_count: int = CHANNELS):
        self.capacity = capacity
        self.channel_count = channel_count
        self.data = np.empty((capacity, channel_count), dtype="<u2")
        self.total = 0
        self.lock = threading.Lock()

    def append(self, raw: np.ndarray) -> int:
        raw = np.asarray(raw, dtype="<u2")
        if raw.ndim != 2 or raw.shape[1] != self.channel_count:
            raise ValueError("Sample columns do not match the selected inputs")
        with self.lock:
            start = self.total
            n = len(raw)
            skip = max(0, n - self.capacity)
            pos = (self.total + skip) % self.capacity
            values = raw[skip:]
            first = min(len(values), self.capacity - pos)
            self.data[pos:pos + first] = values[:first]
            self.data[:len(values) - first] = values[first:]
            self.total += n
            return start

    def snapshot(self, limit: int | None = None) -> tuple[int, np.ndarray]:
        with self.lock:
            n = min(self.total, self.capacity)
            if limit is not None:
                n = min(n, max(0, limit))
            first_index = self.total - n
            pos = first_index % self.capacity
            first = min(n, self.capacity - pos)
            result = np.concatenate((self.data[pos:pos + first], self.data[:n - first]))
            return first_index, result

    def latest(self) -> tuple[int, np.ndarray]:
        """Copy only the newest row for responsive numeric readouts."""
        with self.lock:
            if not self.total:
                return -1, np.empty((0, self.channel_count), dtype="<u2")
            position = (self.total - 1) % self.capacity
            return self.total - 1, self.data[position:position+1].copy()


class FrameBuffer:
    """Keep the most recent entire burst, never stitch USB-separated frames."""

    def __init__(self, channel_count=CHANNELS):
        self.total = 0
        self.frame_count = 0
        self.first_index = 0
        self.channel_count = channel_count
        self.data = np.empty((0, channel_count), dtype="<u2")
        self.lock = threading.Lock()

    def append(self, raw):
        values = np.asarray(raw, dtype="<u2")
        if values.ndim != 2 or values.shape[1] != self.channel_count or not len(values):
            raise ValueError("Frame columns do not match the selected inputs")
        with self.lock:
            self.first_index = self.total
            self.total += len(values)
            self.frame_count += 1
            self.data = values.copy()
            return self.first_index

    def snapshot(self, limit=None):
        with self.lock:
            return self.first_index, self.data.copy()

    def latest(self):
        with self.lock:
            return self.total - 1, self.data[-1:].copy()


def to_volts(raw: np.ndarray, metadata: dict, probe_factors=None) -> np.ndarray:
    zero = np.asarray(metadata["zero_offsets_counts"], dtype=float)
    gains = np.asarray(metadata["volts_per_count"], dtype=float)
    factors = np.ones(len(zero)) if probe_factors is None else np.asarray(probe_factors)
    return (raw.astype(float) - zero) * gains * factors


class CaptureWriter:
    """Each committed batch is recoverable even after an interrupted session."""

    def __init__(self, path: str | Path, metadata: dict, start_index: int):
        order = channel_order(metadata)
        self.channel_count = len(order)
        self.path = Path(path)
        
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE batches (sample_index INTEGER PRIMARY KEY, count INTEGER NOT NULL, received_monotonic_ns INTEGER NOT NULL, raw_u16_le BLOB NOT NULL)")
        meta = dict(metadata, format=FORMAT, recording_start_index=start_index,
                    complete=False, samples_per_channel=0)
        if metadata.get("acquisition_mode") == "burst":
            
            meta["format"] = "hantek-linux-2"
        if order != list(range(1, 9)):
            
            meta["format"] = "hantek-linux-3"
        self.db.executemany("INSERT INTO metadata VALUES (?, ?)",
                            [(key, json.dumps(value)) for key, value in meta.items()])
        self.db.commit()
        self.next_index = start_index
        self.count = 0
        self.closed = False

    def append(self, start: int, raw: np.ndarray, received_ns: int):
        raw = np.ascontiguousarray(raw, dtype="<u2")
        if raw.ndim != 2 or raw.shape[1] != self.channel_count or not len(raw):
            raise ValueError("Invalid capture batch")
        if start != self.next_index:
            raise ValueError(f"Sample discontinuity: expected {self.next_index}, received {start}")
        self.db.execute("INSERT INTO batches VALUES (?, ?, ?, ?)",
                        (int(start), len(raw), int(received_ns), raw.tobytes()))
        self.next_index += len(raw)
        self.count += len(raw)

    def flush(self):
        self.db.commit()

    def set_channel_names(self, names):
        from channel_names import normalize_names
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('channel_names', json.dumps(normalize_names(names))))
        self.flush()

    def close(self, complete=True, error=None):
        if self.closed:
            return
        try:
            for key, value in {"complete": complete, "samples_per_channel": self.count,
                               "error": error}.items():
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.db.close()
            self.closed = True


class CaptureReader:
    """Validate an entire recording, then read bounded windows for playback."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            self.metadata = {k: json.loads(v) for k, v in self.db.execute("SELECT key,value FROM metadata")}
            if self.metadata.get("format") not in {FORMAT, "hantek-linux-2", "hantek-linux-3"}:
                raise ValueError("Not a supported Hantek Linux capture")
            self.mode = self.metadata.get("acquisition_mode", "roll")
            self.channel_order = channel_order(self.metadata)
            self.channel_count = len(self.channel_order)
            if self.mode not in {"roll", "burst"}:
                raise ValueError("Unsupported acquisition mode")
            zero = np.asarray(self.metadata["zero_offsets_counts"], dtype=float)
            scale = np.asarray(self.metadata["volts_per_count"], dtype=float)
            rate = float(self.metadata["sample_rate_nominal_hz"])
            if zero.shape != (self.channel_count,) or scale.shape != (self.channel_count,) or not np.all(np.isfinite(zero)) or not np.all(np.isfinite(scale)) or not np.isfinite(rate) or rate <= 0:
                raise ValueError("Invalid acquisition metadata")
            self.start = int(self.metadata["recording_start_index"])
            expected = self.start
            for index, n, length in self.db.execute("SELECT sample_index,count,length(raw_u16_le) FROM batches ORDER BY sample_index"):
                if index != expected or n <= 0 or length != n * self.channel_count * 2:
                    raise ValueError("Capture has a missing or malformed sample batch")
                expected += n
            self.end = expected
            self.count = self.end - self.start
            if self.metadata.get("complete") and self.count != self.metadata.get("samples_per_channel"):
                raise ValueError("Capture sample count does not match its metadata")
            self.frames = list(self.db.execute("SELECT sample_index,count FROM batches ORDER BY sample_index")) if self.mode == "burst" else []
        except Exception:
            self.db.close()
            raise

    def window(self, start: int, stop: int) -> tuple[int, np.ndarray]:
        start, stop = max(self.start, start), min(self.end, stop)
        if stop <= start:
            return start, np.empty((0, self.channel_count), dtype="<u2")
        arrays = []
        for index, n, blob in self.db.execute(
            "SELECT sample_index,count,raw_u16_le FROM batches WHERE sample_index < ? AND sample_index+count > ? ORDER BY sample_index", (stop, start)
        ):
            values = np.frombuffer(blob, dtype="<u2").reshape(n, self.channel_count)
            arrays.append(values[max(0, start-index):min(n, stop-index)])
        return start, np.concatenate(arrays) if arrays else np.empty((0, self.channel_count), dtype="<u2")

    def close(self):
        self.db.close()

    def frame(self, position):
        start, n = self.frames[position]
        return self.window(start, start+n)
