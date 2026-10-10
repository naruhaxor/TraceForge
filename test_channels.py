"""Channel identity, selective recordings and long time windows."""
from pathlib import Path
import tempfile
import unittest
import numpy as np

from core import (CaptureReader, CaptureWriter, SampleBuffer, FrameBuffer,
                  nominal_rate, scan_channels, format_time, to_volts)


class ChannelTests(unittest.TestCase):
    def test_nonadjacent_channels_survive_recording_and_conversion(self):
        meta = {"channel_order": [3, 7], "zero_offsets_counts": [2020., 2040.],
                "volts_per_count": [.01, .01], "probe_factors_at_record_start": [1, 10],
                "sample_rate_nominal_hz": 1_200_000, "acquisition_mode": "burst"}
        raw = np.array([[2153, 2050], [1887, 2030]], dtype="<u2")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"two-inputs.hscope"
            writer = CaptureWriter(path, meta, 1200)
            writer.append(1200, raw, 99)
            writer.close()
            reader = CaptureReader(path)
            self.assertEqual(reader.channel_order, [3, 7])
            self.assertEqual(reader.metadata["format"], "hantek-linux-3")
            self.assertTrue(reader.metadata["complete"])
            np.testing.assert_array_equal(reader.frame(0)[1], raw)
            np.testing.assert_allclose(to_volts(raw, reader.metadata, [1, 10]), [[1.33, 1.], [-1.33, -1.]])
            self.assertEqual(reader.window(0, 0)[1].shape, (0, 2))
            reader.close()

    def test_sparse_frame_and_ten_minute_ring_keep_sample_order(self):
        rate = nominal_rate("roll", [0, 2])
        capacity = int(np.ceil(rate*600))
        raw = (np.arange((capacity+123)*2, dtype=np.uint32) % 4096).astype("<u2").reshape(-1, 2)
        ring = SampleBuffer(capacity, 2)
        ring.append(raw[:2000])
        ring.append(raw[2000:])
        start, actual = ring.snapshot()
        self.assertEqual(start, 123)
        self.assertGreaterEqual(len(actual)/rate, 600)
        np.testing.assert_array_equal(actual, raw[-capacity:])
        frame = FrameBuffer(2)
        frame.append(raw[:1000])
        frame.append(raw[1000:2000])
        self.assertEqual(frame.snapshot()[0], 1000)
        np.testing.assert_array_equal(frame.snapshot()[1], raw[1000:2000])

    def test_vendor_padding_affects_rate_but_not_selected_identity(self):
        for selected, expected in [([2], [2]), ([0, 2], [0, 2]),
                                   ([2, 4, 6], [0, 2, 4, 6]),
                                   ([1, 2, 4, 5, 7], [0, 1, 2, 4, 5, 7]),
                                   (list(range(1, 8)), list(range(8)))]:
            self.assertEqual(scan_channels(selected, "burst"), expected)
            self.assertEqual(scan_channels(selected, "roll"), selected)
            self.assertAlmostEqual(nominal_rate("burst", selected), 2_400_000/len(expected))
        self.assertAlmostEqual(nominal_rate("burst", [0, 2], 5_000_000), 37500)
        self.assertEqual(format_time(60), "1 min")
        with self.assertRaises(ValueError):
            scan_channels([], "burst")
        with self.assertRaises(ValueError):
            scan_channels([0, 0], "roll")

    def test_six_slot_partial_tail_does_not_create_fake_samples(self):
        from acquisition import Acquisition
        selected = [1, 2, 4, 5, 7]
        worker = Acquisition(FrameBuffer(5), mode="burst", channels=selected)
        slots = worker.scan_channels
        counts = np.arange(4000).tolist()
        class Scope:
            def request_samples_burst_mode(self):
                return {ch: counts[i::6] for i, ch in enumerate(slots)}
        stream = worker.burst_frames(Scope())
        frame = next(stream)
        stream.close()
        self.assertEqual({len(values) for values in frame.values()}, {666})
        for i, ch in enumerate(slots):
            self.assertEqual(frame[ch], counts[i:3996:6])


if __name__ == "__main__":
    unittest.main()
