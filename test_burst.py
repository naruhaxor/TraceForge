"""Fast-frame storage and timing regressions; no instrument required."""
from pathlib import Path
import tempfile
import unittest
import numpy as np

from core import BURST_RATE, FrameBuffer, CaptureReader, CaptureWriter, format_time
from test_core import META


class BurstTests(unittest.TestCase):
    def test_latest_frame_never_contains_previous_frame(self):
        buffer = FrameBuffer()
        buffer.append(np.full((500,8),1111,dtype="<u2"))
        buffer.append(np.full((500,8),2222,dtype="<u2"))
        start, values = buffer.snapshot(0)
        self.assertEqual(start,500)
        self.assertEqual(buffer.total,1000)
        self.assertEqual(buffer.frame_count,2)
        np.testing.assert_array_equal(values,np.full((500,8),2222))

    def test_recorded_frames_retain_boundaries_and_host_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"frames.hscope"
            meta=dict(META, acquisition_mode="burst", sample_rate_nominal_hz=BURST_RATE)
            writer=CaptureWriter(path,meta,500)
            a=np.arange(4000,dtype="<u2").reshape(500,8)
            b=a+1
            writer.append(500,a,1_000_000_000)
            writer.append(1000,b,9_000_000_000)
            writer.close()
            reader=CaptureReader(path)
            self.assertEqual(reader.metadata["format"],"hantek-linux-2")
            self.assertEqual(reader.frames,[(500,500),(1000,500)])
            np.testing.assert_array_equal(reader.frame(0)[1],a)
            np.testing.assert_array_equal(reader.frame(1)[1],b)
            timestamps=list(reader.db.execute("SELECT received_monotonic_ns FROM batches ORDER BY sample_index"))
            self.assertEqual(timestamps,[(1_000_000_000,),(9_000_000_000,)])
            reader.close()

    def test_time_units_and_nominal_spacing(self):
        self.assertEqual(format_time(.00002),"20 µs")
        self.assertEqual(format_time(.005),"5 ms")
        self.assertEqual(format_time(2),"2 s")
        self.assertAlmostEqual(1/BURST_RATE*1e6,10/3)


if __name__=="__main__":
    unittest.main()
