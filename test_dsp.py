import unittest
import numpy as np
from dsp import Processor


class DSPTests(unittest.TestCase):
    def test_inversion_and_stereo(self):
        p = Processor(48000)
        p.gain = -.5
        x = np.array([[.1], [-.2], [.3]], dtype=np.float32)
        y = np.empty((3, 2), dtype=np.float32)
        p.process(x, y, -.5)
        np.testing.assert_allclose(y[:, 0], -.5 * x[:, 0])
        np.testing.assert_array_equal(y[:, 0], y[:, 1])

    def test_delay_across_varying_callbacks(self):
        for delay in [1, 7, 31]:
            p = Processor(1000, delay)
            p.gain = -1
            x = np.linspace(-.2, .2, 70, dtype=np.float32)
            chunks = []
            offset = 0
            for count in [3, 20, 1, 46]:
                y = np.empty((count, 2), dtype=np.float32)
                p.process(x[offset:offset+count, None], y, -1)
                chunks.append(y[:, 0].copy())
                offset += count
            expected = np.concatenate([np.zeros(delay), -x])[:len(x)]
            np.testing.assert_allclose(np.concatenate(chunks), expected)

    def test_startup_ramp_and_polarity_slew(self):
        p = Processor(1000)
        x = np.full((40, 1), .1, dtype=np.float32)
        y = np.empty_like(x)
        p.process(x, y, -1)
        self.assertLess(abs(y[0, 0]), .01)
        self.assertAlmostEqual(float(y[-1, 0]), -.1)
        p.process(x, y, 1)
        self.assertLess(float(np.max(np.abs(np.diff(y[:, 0])))), .006)
        self.assertAlmostEqual(float(y[-1, 0]), .1)

    def test_clipping_and_nonfinite(self):
        p = Processor(48000)
        p.gain = 1
        y = np.empty((5, 2), dtype=np.float32)
        p.process(np.array([[9], [-9], [np.nan], [np.inf], [-np.inf]], dtype=np.float32), y, 1)
        self.assertTrue(np.isfinite(y).all())
        self.assertLessEqual(float(np.max(np.abs(y))), .25)
        self.assertTrue(p.limited)

    def test_capacity_guard(self):
        p = Processor(48000, max_frames=2)
        with self.assertRaises(ValueError):
            p.process(np.zeros((3, 1)), np.empty((3, 2)), 1)

    def test_threshold_passes_quiet_and_mutes_loud(self):
        p = Processor(1000)
        p.gain = -1
        y = np.empty((10, 2), dtype=np.float32)
        p.process(np.full((10, 1), .01), y, -1, -30)
        np.testing.assert_allclose(y, -.01)
        loud = np.zeros((10, 1))
        loud[5] = -.1  # Negative peaks must trigger too; whole block is muted.
        p.process(loud, y, -1, -30)
        np.testing.assert_array_equal(y, 0)
        self.assertTrue(p.gated)

    def test_threshold_hold_retrigger_and_recovery(self):
        p = Processor(1000)
        y = np.empty((100, 2), dtype=np.float32)
        quiet = np.full((100, 1), .01)
        loud = np.full((100, 1), .1)
        p.process(loud, y, -1, -30)
        p.process(quiet, y, -1, -30)
        p.process(loud, y, -1, -30)
        for _ in range(2):
            p.process(quiet, y, -1, -30)
            np.testing.assert_array_equal(y, 0)
        p.process(quiet, y, -1, -30)
        self.assertFalse(p.gated)
        self.assertLess(abs(y[0, 0]), .001)
        self.assertAlmostEqual(float(y[-1, 0]), -.01)

    def test_threshold_clears_delayed_audio_and_can_be_disabled(self):
        p = Processor(1000, delay_ms=20)
        y = np.empty((10, 2), dtype=np.float32)
        p.process(np.full((10, 1), .01), y, -1, -30)
        p.process(np.full((10, 1), .1), y, -1, -30)
        np.testing.assert_array_equal(p.ring, 0)
        p.process(np.zeros((10, 1)), y, -1, None)
        self.assertFalse(p.gated)
        np.testing.assert_array_equal(y, 0)

    def test_threshold_equality_and_live_change(self):
        p = Processor(1000)
        y = np.empty((10, 2), dtype=np.float32)
        x = np.full((10, 1), .125)
        p.process(x, y, -1, 20 * np.log10(.125))
        self.assertTrue(p.gated)
        p.process(x, y, -1, None)
        self.assertFalse(p.gated)
        p.process(x, y, -1, -40)
        self.assertTrue(p.gated)


if __name__ == '__main__':
    unittest.main()
