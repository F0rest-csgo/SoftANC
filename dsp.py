"""Bounded, preallocated audio processing; no UI, I/O or locks in process()."""
import numpy as np


class Processor:
    def __init__(self, samplerate, delay_ms=0, max_frames=32768):
        self.delay = int(round(samplerate * delay_ms / 1000))
        if not 0 <= self.delay <= samplerate // 10:
            raise ValueError('Delay must be between 0 and 100 ms')
        self.capacity = max_frames
        self.ring = np.zeros(max(1, self.delay), dtype=np.float32)
        self.pos = 0
        self.work = np.zeros(max_frames, dtype=np.float32)
        self.ramp = np.zeros(max_frames, dtype=np.float32)
        self.indices = np.arange(1, max_frames + 1, dtype=np.float32)
        self.gain = 0.0
        self.ramp_samples = max(1, int(samplerate * .02))
        self.input_peak = self.output_peak = 0.0
        self.limited = False
        self.hold_samples = int(samplerate * .2)
        self.hold_remaining = 0
        self.gated = False

    def process(self, source, destination, target_gain, threshold_db=None):
        n = len(source)
        if n > self.capacity or not np.isfinite(target_gain):
            raise ValueError('Invalid callback size or gain')
        mono = source[:, 0]
        self.input_peak = max(abs(float(mono.min())), abs(float(mono.max())))
        if threshold_db is not None:
            if not np.isfinite(threshold_db) or not -80 <= threshold_db <= 0:
                raise ValueError('Threshold must be between -80 and 0 dBFS')
            threshold = 10 ** (threshold_db / 20)
            if not np.isfinite(self.input_peak) or self.input_peak >= threshold:
                self.hold_remaining = self.hold_samples
                self.gated = True
            elif self.hold_remaining > 0:
                self.hold_remaining = max(0, self.hold_remaining - n)
                self.gated = True
            else:
                self.gated = False
        else:
            self.hold_remaining = 0
            self.gated = False
        if self.gated:
            # Mute the whole triggering block, before delay buffering. Clear old
            # samples so speech cannot reappear when the gate opens again.
            destination.fill(0)
            self.ring.fill(0)
            self.pos = 0
            self.gain = 0.0
            self.output_peak = 0.0
            self.limited = False
            return
        work = self.work[:n]
        # A delay-sized ring also handles callbacks larger than the delay.
        if self.delay:
            offset = 0
            while offset < n:
                count = min(n - offset, self.delay - self.pos)
                work[offset:offset + count] = self.ring[self.pos:self.pos + count]
                self.ring[self.pos:self.pos + count] = mono[offset:offset + count]
                self.pos = (self.pos + count) % self.delay
                offset += count
        else:
            np.copyto(work, mono)
        # Slew at a bounded rate, including polarity changes and startup.
        delta = float(np.clip(target_gain, -1, 1)) - self.gain
        step = float(np.clip(delta, -1 / self.ramp_samples, 1 / self.ramp_samples))
        ramp = self.ramp[:n]
        np.multiply(self.indices[:n], step, out=ramp)
        if delta >= 0:
            np.minimum(ramp, delta, out=ramp)
        else:
            np.maximum(ramp, delta, out=ramp)
        ramp += self.gain
        self.gain = float(ramp[-1])
        np.multiply(work, ramp, out=work)
        np.nan_to_num(work, copy=False, nan=0, posinf=0, neginf=0)
        peak = max(abs(float(work.min())), abs(float(work.max())))
        self.limited = peak > .25
        # Digital ceiling does not guarantee a safe acoustic sound pressure.
        np.clip(work, -.25, .25, out=work)
        self.output_peak = min(peak, .25)
        destination[:] = work[:, None]
