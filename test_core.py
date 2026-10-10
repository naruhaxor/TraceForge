"""Regression checks for sample order, capture recovery and conversion."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np

from core import CaptureReader, CaptureWriter, SampleBuffer, to_volts

META = {"zero_offsets_counts": [2048.25]*8, "volts_per_count": [.01]*8,
        "sample_rate_nominal_hz": 440, "probe_factors_at_record_start": [1]*8}


class CoreTests(unittest.TestCase):
    def test_ring_order_across_wraps(self):
        buffer = SampleBuffer(11)
        raw = np.arange(31*8, dtype="<u2").reshape(31, 8)
        buffer.append(raw[:7])
        buffer.append(raw[7:15])
        buffer.append(raw[15:])
        start, actual = buffer.snapshot()
        self.assertEqual(start, 20)
        np.testing.assert_array_equal(actual, raw[20:])
        self.assertEqual(buffer.snapshot(4)[0], 27)
        np.testing.assert_array_equal(buffer.snapshot(4)[1], raw[-4:])

    def test_snapshot_is_independent(self):
        buffer = SampleBuffer(4)
        buffer.append(np.ones((4,8), dtype="<u2"))
        _, saved = buffer.snapshot()
        buffer.append(np.full((4,8), 9, dtype="<u2"))
        np.testing.assert_array_equal(saved, np.ones((4,8)))

    def test_conversion_retains_fractional_zero_and_probe_scale(self):
        raw = np.full((2,8), 2207, dtype="<u2")
        actual = to_volts(raw, META, [10,1,1,1,1,1,1,1])
        self.assertAlmostEqual(actual[0,0], 15.875)
        self.assertAlmostEqual(actual[0,1], 1.5875)

    def test_recording_round_trip_and_bounded_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test.hscope"
            writer = CaptureWriter(path, META, 40)
            raw = np.arange(20*8, dtype="<u2").reshape(20,8)
            writer.append(40, raw[:7], 123456)
            writer.append(47, raw[7:], 456789)
            writer.close()
            reader = CaptureReader(path)
            self.assertEqual(reader.count, 20)
            self.assertTrue(reader.metadata["complete"])
            start, window = reader.window(43, 52)
            self.assertEqual(start, 43)
            np.testing.assert_array_equal(window, raw[3:12])
            self.assertEqual(len(reader.window(100,120)[1]), 0)
            reader.close()
            with self.assertRaises(FileExistsError):
                CaptureWriter(path, META, 0)

    def test_sample_gap_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = CaptureWriter(Path(directory)/"test.hscope", META, 0)
            with self.assertRaises(ValueError):
                writer.append(1, np.zeros((3,8), dtype="<u2"), 1)
            writer.close(complete=False)

    def test_interrupted_capture_recovers_committed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test.hscope"
            writer = CaptureWriter(path, META, 12)
            writer.append(12, np.full((7,8), 2222, dtype="<u2"), 10)
            writer.flush()
            
            writer.db.close()
            reader = CaptureReader(path)
            self.assertFalse(reader.metadata["complete"])
            self.assertEqual(reader.count, 7)
            np.testing.assert_array_equal(reader.window(12,19)[1], np.full((7,8),2222))
            reader.close()

    def test_malformed_blob_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test.hscope"
            writer = CaptureWriter(path, META, 0)
            writer.append(0, np.zeros((3,8), dtype="<u2"), 10)
            writer.close()
            db = sqlite3.connect(path)
            db.execute("UPDATE batches SET raw_u16_le=?", (b"broken",))
            db.commit()
            db.close()
            with self.assertRaises(ValueError):
                CaptureReader(path)

    def test_every_decoded_vendor_trace_matches_index(self):
        root = Path(__file__).parent / "references"
        entries = json.loads((root/"reference-index.json").read_text())
        traces = 0
        with np.load(root/"reference-waveforms.npz", allow_pickle=False) as arrays:
            decoded = [e for e in entries if e.get("status")=="decoded"]
            self.assertEqual(len(decoded), 86)
            for entry in decoded:
                for trace in entry["traces"]:
                    data = arrays[trace["npz_key"]]
                    self.assertEqual(len(data), entry["samples_per_trace"])
                    self.assertTrue(np.all(np.isfinite(data)))
                    traces += 1
            self.assertEqual(traces, 121)


if __name__ == "__main__":
    unittest.main()
