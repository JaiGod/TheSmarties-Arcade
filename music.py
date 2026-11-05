"""
Study-music generator for TheSmarties Arcade.

Everything here is made from maths (sine waves and shaped noise) - there are no
audio files and no outside service. render() returns a loop of raw audio that
Discord can play: 48 kHz, stereo, 16-bit.
"""

import numpy as np

SR = 48000
STYLES = ("lofi", "ambient", "focus", "classical")
INTENSITIES = ("calm", "focus", "energetic")
AMBIENCES = ("none", "rain", "cafe", "white")


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


class Loop:
    """A stereo buffer where anything that runs past the end wraps to the start,
    so the result repeats with no click."""

    def __init__(self, seconds: float):
        self.n = int(seconds * SR)
        self.buf = np.zeros((2, self.n), dtype=np.float64)

    def add(self, start_sec: float, sig: np.ndarray, gain: float = 1.0, pan: float = 0.0):
        left = gain * (1 - max(0.0, pan))
        right = gain * (1 + min(0.0, pan))
        start = int(start_sec * SR) % self.n
        pos = 0
        while pos < len(sig):
            take = min(len(sig) - pos, self.n - start)
            chunk = sig[pos:pos + take]
            self.buf[0, start:start + take] += chunk * left
            self.buf[1, start:start + take] += chunk * right
            pos += take
            start = 0


def _t(dur: float) -> np.ndarray:
    return np.arange(int(dur * SR)) / SR


def pad(freq: float, dur: float) -> np.ndarray:
    """Soft, slow-swelling synth pad."""
    t = _t(dur)
    sig = (np.sin(2 * np.pi * freq * t) + 0.6 * np.sin(2 * np.pi * freq * 1.004 * t)
           + 0.6 * np.sin(2 * np.pi * freq * 0.996 * t) + 0.2 * np.sin(2 * np.pi * freq * 2 * t))
    attack, release = int(len(t) * 0.3), int(len(t) * 0.4)
    env = np.ones(len(t))
    env[:attack] = 0.5 - 0.5 * np.cos(np.pi * np.arange(attack) / attack)
    env[-release:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(release) / release)
    return sig * env / 2.4


def keys(freq: float, dur: float, decay: float = 3.0) -> np.ndarray:
    """Electric-piano / harp style note: quick start, fades out."""
    t = _t(dur)
    sig = (np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2 * t)
           + 0.12 * np.sin(2 * np.pi * freq * 3 * t))
    env = np.exp(-decay * t / dur * 1.5)
    ramp = min(len(t), int(0.006 * SR))
    env[:ramp] *= np.linspace(0, 1, ramp)
    tail = min(len(t), int(0.02 * SR))
    env[-tail:] *= np.linspace(1, 0, tail)
    return sig * env / 1.47


def kick() -> np.ndarray:
    t = _t(0.28)
    freq = 45 + 75 * np.exp(-t * 28)
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 16)


def snare(rng) -> np.ndarray:
    t = _t(0.18)
    noise = rng.standard_normal(len(t))
    noise = np.convolve(noise, np.ones(4) / 4, mode="same")
    return (0.7 * noise * np.exp(-t * 26) + 0.4 * np.sin(2 * np.pi * 185 * t) * np.exp(-t * 34))


def hat(rng) -> np.ndarray:
    t = _t(0.05)
    noise = np.diff(rng.standard_normal(len(t) + 1))
    return noise * np.exp(-t * 90) * 0.5


def shaped_noise(n: int, rng, shape) -> np.ndarray:
    """Noise with a chosen tone. Built in the frequency domain, so it loops perfectly."""
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    freqs[0] = 1.0
    out = np.fft.irfft(spectrum * shape(freqs), n)
    return out / (np.abs(out).max() + 1e-9)


def slow_wobble(n: int, rng, speed_hz: float, depth: float) -> np.ndarray:
    """A slowly changing volume curve (also loops perfectly)."""
    curve = shaped_noise(n, rng, lambda f: (f < speed_hz).astype(float))
    return 1 - depth + depth * (0.5 + 0.5 * curve)


def ambience_layer(kind: str, n: int, rng) -> np.ndarray:
    """Returns a (2, n) stereo background layer."""
    if kind == "none":
        return np.zeros((2, n))
    seconds = n / SR
    out = Loop(seconds)
    if kind == "white":
        for ch in (0, 1):  # soft "pink" noise, easier on the ears than pure white
            out.buf[ch] += shaped_noise(n, rng, lambda f: 1 / np.sqrt(f)) * 0.5
    elif kind == "rain":
        for ch in (0, 1):
            hiss = shaped_noise(n, rng, lambda f: np.exp(-((np.log(f) - np.log(3500)) ** 2) / 1.6))
            out.buf[ch] += hiss * slow_wobble(n, rng, 0.4, 0.35) * 0.45
        drop_t = _t(0.03)
        for _ in range(int(seconds * 14)):  # individual drops
            drop = np.sin(2 * np.pi * rng.uniform(1500, 5000) * drop_t) * np.exp(-drop_t * 160)
            out.add(rng.uniform(0, seconds), drop, gain=rng.uniform(0.05, 0.22), pan=rng.uniform(-0.8, 0.8))
    elif kind == "cafe":
        for ch in (0, 1):  # murmur of voices
            murmur = shaped_noise(n, rng, lambda f: np.exp(-((np.log(f) - np.log(450)) ** 2) / 0.9))
            out.buf[ch] += murmur * slow_wobble(n, rng, 1.5, 0.6) * 0.5
            out.buf[ch] += shaped_noise(n, rng, lambda f: 1 / f) * 0.15  # room rumble
        clink_t = _t(0.25)
        for _ in range(int(seconds / 5)):  # cups and spoons
            f0 = rng.uniform(2200, 4200)
            clink = (np.sin(2 * np.pi * f0 * clink_t) + 0.5 * np.sin(2 * np.pi * f0 * 2.4 * clink_t)) * np.exp(-clink_t * 30)
            out.add(rng.uniform(0, seconds), clink, gain=rng.uniform(0.03, 0.09), pan=rng.uniform(-0.9, 0.9))
    return out.buf


LOFI_CHORDS = [(50, 53, 57, 60), (55, 59, 62, 65), (48, 52, 55, 59), (45, 48, 52, 55)]      # Dm7 G7 Cmaj7 Am7
AMBIENT_CHORDS = [(48, 55, 62, 64, 71), (45, 52, 59, 60, 67), (41, 48, 55, 57, 64), (43, 50, 57, 60, 67)]
CLASSICAL_CHORDS = [(50, 54, 57), (45, 49, 52), (47, 50, 54), (42, 45, 49),                 # D A Bm F#m
                    (43, 47, 50), (50, 54, 57), (43, 47, 50), (45, 49, 52)]                 # G D G A
FOCUS_CHORDS = [(48, 55, 62, 64), (45, 52, 60, 64), (41, 53, 57, 64), (43, 55, 59, 62)]
C_PENTATONIC = [72, 74, 76, 79, 81, 84]
D_MAJOR = [74, 76, 78, 79, 81, 83, 85, 86]


def music_layer(style: str, intensity: str, rng):
    """Returns (stereo buffer, loop length in seconds)."""
    level = INTENSITIES.index(intensity)          # 0 calm, 1 focus, 2 energetic
    bpm = {"lofi": (66, 74, 86), "ambient": (48, 54, 60), "focus": (60, 70, 84), "classical": (56, 66, 80)}[style][level]
    beat = 60 / bpm
    bar = beat * 4
    bars = 16
    loop = Loop(bars * bar)
    busy = (0.25, 0.4, 0.6)[level]                # how often the melody plays

    if style == "lofi":
        swing = beat * 0.08
        note = C_PENTATONIC[2]
        for b in range(bars):
            chord = LOFI_CHORDS[b % 4]
            start = b * bar
            for i, m in enumerate(chord):
                loop.add(start, pad(hz(m), bar * 1.1), gain=0.10, pan=(i - 1.5) * 0.25)
                loop.add(start + 0.01 * i, keys(hz(m + 12), beat * 2), gain=0.13, pan=-0.2)
                loop.add(start + beat * 2.5 + swing, keys(hz(m + 12), beat * 1.5), gain=0.09, pan=0.2)
            loop.add(start, keys(hz(chord[0] - 12), beat * 2, decay=2), gain=0.30)
            loop.add(start + beat * 2, keys(hz(chord[0] - 12), beat * 1.5, decay=2), gain=0.22)
            drum = (0.5, 0.75, 1.0)[level]
            loop.add(start, kick(), gain=0.45 * drum)
            loop.add(start + beat * 2.5 + swing, kick(), gain=0.32 * drum)
            loop.add(start + beat, snare(rng), gain=0.16 * drum)
            loop.add(start + beat * 3, snare(rng), gain=0.16 * drum)
            for e in range(8):
                when = start + e * beat / 2 + (swing if e % 2 else 0)
                loop.add(when, hat(rng), gain=(0.05 if e % 2 else 0.08) * drum, pan=0.3)
                if rng.random() < busy:
                    note = int(np.clip(C_PENTATONIC.index(note) + rng.integers(-2, 3), 0, 5))
                    note = C_PENTATONIC[note]
                    loop.add(when, keys(hz(note), beat * 1.5, decay=4), gain=0.12, pan=rng.uniform(-0.4, 0.4))
        crackle = np.zeros(loop.n)                 # vinyl crackle
        pops = rng.integers(0, loop.n, int(loop.n / SR * 9))
        crackle[pops] = rng.uniform(-1, 1, len(pops))
        loop.buf += np.convolve(crackle, np.ones(3) / 3, mode="same") * 0.05

    elif style == "ambient":
        for b in range(0, bars, 2):
            chord = AMBIENT_CHORDS[(b // 2) % 4]
            for i, m in enumerate(chord):
                loop.add(b * bar, pad(hz(m), bar * 3.2), gain=0.13, pan=(i - 2) * 0.3)
            for _ in range(1 + level):
                if rng.random() < 0.8:
                    m = chord[rng.integers(1, len(chord))] + 24
                    loop.add(b * bar + rng.uniform(0, bar * 2), keys(hz(m), bar * 1.5, decay=2.5),
                             gain=0.07, pan=rng.uniform(-0.7, 0.7))

    elif style == "focus":
        for m, g in ((36, 0.20), (43, 0.14), (48, 0.10)):   # steady drone under everything
            cycles = round(hz(m) * loop.n / SR)
            t = np.arange(loop.n) / loop.n
            loop.buf += np.sin(2 * np.pi * cycles * t) * g
        for b in range(bars):
            chord = FOCUS_CHORDS[(b // 2) % 4]
            start = b * bar
            if b % 2 == 0:
                for i, m in enumerate(chord):
                    loop.add(start, pad(hz(m), bar * 2.2), gain=0.09, pan=(i - 1.5) * 0.3)
            steps = 8 if level < 2 else 16
            for s in range(steps):                 # gentle repeating pulse
                m = chord[(s * 3) % len(chord)] + 12
                loop.add(start + s * bar / steps, keys(hz(m), bar / steps * 2, decay=4),
                         gain=0.06 + 0.02 * (s % 4 == 0), pan=0.5 if s % 2 else -0.5)
            if level == 2:
                for s in range(8):
                    loop.add(start + s * beat / 2, hat(rng), gain=0.04, pan=-0.2)

    elif style == "classical":
        pattern = (0, 1, 2, 1, 2, 1, 0, 1)
        note = 4
        for b in range(bars):
            chord = CLASSICAL_CHORDS[b % 8]
            start = b * bar
            loop.add(start, keys(hz(chord[0] - 12), bar, decay=2), gain=0.24)
            loop.add(start + beat * 2, keys(hz(chord[2] - 12), beat * 2, decay=2), gain=0.16)
            steps = 8 if level < 2 else 16
            for s in range(steps):                 # harp-style broken chords
                m = chord[pattern[s % 8]] + 12
                loop.add(start + s * bar / steps, keys(hz(m), beat * 1.6, decay=3.5), gain=0.11,
                         pan=-0.3 + 0.1 * pattern[s % 8])
            for q in range(4):                     # melody moving by step
                if rng.random() < busy + 0.3:
                    note = int(np.clip(note + rng.integers(-2, 3), 0, len(D_MAJOR) - 1))
                    loop.add(start + q * beat, keys(hz(D_MAJOR[note]), beat * 2.2, decay=2.5), gain=0.13, pan=0.25)
            for i, m in enumerate(chord):
                loop.add(start, pad(hz(m + 12), bar * 1.1), gain=0.035, pan=(i - 1) * 0.4)
    else:
        raise ValueError(f"unknown style {style!r}")

    return loop.buf, loop.n / SR


def render(style: str = "lofi", intensity: str = "calm", ambience: str = "none", seed: int = 7) -> bytes:
    """Make one seamless loop and return it as raw 16-bit stereo audio."""
    rng = np.random.default_rng(seed)
    music, _ = music_layer(style, intensity, rng)
    music *= 0.13 / (np.sqrt((music ** 2).mean()) + 1e-9)   # same loudness for every style
    mix = music + ambience_layer(ambience, music.shape[1], rng) * 0.2
    mix = np.tanh(mix) * 0.9                       # gentle limiter so nothing clips
    return (mix.T * 32767).astype("<i2").tobytes()
