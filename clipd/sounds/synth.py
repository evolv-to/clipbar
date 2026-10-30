"""Synthesize candidate ClipBar 'copy' cues to /tmp/clipbar-sounds/."""
import math
import random
import struct
import wave
from pathlib import Path

SR = 44100
OUT = Path("/tmp/clipbar-sounds")
OUT.mkdir(exist_ok=True)


def write_wav(name: str, samples: list[float]) -> Path:
    path = OUT / f"{name}.wav"
    # Normalize to -1 dBFS so all candidates feel similar in loudness.
    peak = max(1e-9, max(abs(s) for s in samples))
    scale = 0.89 / peak
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        data = b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * scale * 32767))))
            for s in samples
        )
        w.writeframes(data)
    return path


def biquad_lowpass(samples, cutoff_hz):
    # Simple one-pole lowpass — good enough for shaping noise.
    rc = 1.0 / (2 * math.pi * cutoff_hz)
    dt = 1.0 / SR
    a = dt / (rc + dt)
    out = []
    prev = 0.0
    for s in samples:
        prev = prev + a * (s - prev)
        out.append(prev)
    return out


def biquad_highpass(samples, cutoff_hz):
    rc = 1.0 / (2 * math.pi * cutoff_hz)
    dt = 1.0 / SR
    a = rc / (rc + dt)
    out = []
    prev_in = 0.0
    prev_out = 0.0
    for s in samples:
        y = a * (prev_out + s - prev_in)
        out.append(y)
        prev_in = s
        prev_out = y
    return out


def noise(n):
    return [random.random() * 2 - 1 for _ in range(n)]


# 1. Pin tack — sharp transient with 2.5 kHz body resonance, ~40 ms.
def pin_tack():
    n = int(SR * 0.05)
    nz = noise(n)
    out = []
    for i in range(n):
        t = i / SR
        env = math.exp(-t * 220)
        ping = math.sin(2 * math.pi * 2600 * t) * math.exp(-t * 90)
        out.append(0.7 * nz[i] * env + 0.35 * ping)
    return biquad_highpass(out, 600)


# 2. Paper grab — filtered noise with quick attack + slow tail, ~160 ms.
def paper_grab():
    n = int(SR * 0.18)
    nz = noise(n)
    bp = biquad_lowpass(biquad_highpass(nz, 1200), 5000)
    out = []
    for i in range(n):
        t = i / SR
        if t < 0.006:
            env = t / 0.006
        elif t < 0.12:
            env = 1.0 - 0.4 * (t - 0.006) / 0.114
        else:
            env = max(0.0, 0.6 * (1 - (t - 0.12) / 0.06))
        # Slight amplitude wobble — like the rasp of fingers on paper.
        wobble = 1.0 + 0.25 * math.sin(2 * math.pi * 35 * t)
        out.append(bp[i] * env * wobble)
    return out


# 3. Scissor snip — two clicks 35 ms apart.
def snip():
    n = int(SR * 0.1)
    out = [0.0] * n
    for offset in (0, int(SR * 0.035)):
        for i in range(int(SR * 0.012)):
            idx = offset + i
            if idx >= n:
                break
            t = i / SR
            env = math.exp(-t * 380)
            ping = math.sin(2 * math.pi * 3200 * t) * math.exp(-t * 200)
            out[idx] += (random.random() * 2 - 1) * env * 0.6 + ping * 0.3
    return biquad_highpass(out, 800)


# 4. Sticky-note peel — high-frequency rip with rise-fall.
def sticky_peel():
    n = int(SR * 0.22)
    nz = noise(n)
    hp = biquad_highpass(nz, 2500)
    out = []
    for i in range(n):
        t = i / SR
        if t < 0.05:
            env = t / 0.05
        else:
            env = max(0.0, 1.0 - (t - 0.05) / 0.17)
        # Rising pitch suggestion via amplitude modulation.
        mod = 1.0 + 0.3 * math.sin(2 * math.pi * (50 + 200 * t) * t)
        out.append(hp[i] * env * mod * 0.6)
    return out


# 5. Rubber-stamp thud — low body + click on top, ~120 ms.
def stamp():
    n = int(SR * 0.13)
    out = []
    for i in range(n):
        t = i / SR
        thump = math.sin(2 * math.pi * 95 * t) * math.exp(-t * 38)
        thump += math.sin(2 * math.pi * 180 * t) * math.exp(-t * 55) * 0.4
        click = (random.random() * 2 - 1) * math.exp(-t * 260)
        out.append(thump * 0.7 + click * 0.4)
    return out


# 7. Photocopy zip — frequency-swept whirr + mechanical noise + terminator click.
def photocopy_zip():
    dur = 0.32
    n = int(SR * dur)
    nz = biquad_lowpass(biquad_highpass(noise(n), 700), 3200)
    out = []
    for i in range(n):
        t = i / SR
        # Motor pitch sweep — mimics scanner head accelerating then settling.
        freq = 380 + 950 * min(1.0, t / 0.22)
        sweep = math.sin(2 * math.pi * freq * t)
        rumble = math.sin(2 * math.pi * 72 * t) * 0.6
        if t < 0.018:
            env = t / 0.018
        elif t < 0.26:
            env = 1.0 - 0.25 * (t - 0.018) / 0.242
        else:
            env = max(0.0, 0.75 * (1 - (t - 0.26) / 0.06))
        out.append((sweep * 0.35 + nz[i] * 0.4 + rumble * 0.25) * env)
    # Terminator click — head hitting the end of travel.
    click_at = int(SR * 0.285)
    for i in range(int(SR * 0.018)):
        idx = click_at + i
        if idx < n:
            t = i / SR
            out[idx] += (random.random() * 2 - 1) * math.exp(-t * 240) * 0.7
    return biquad_highpass(out, 55)


# 8. Camera shutter — mirror slap + shutter close, two transients ~70 ms apart.
def camera_shutter():
    n = int(SR * 0.16)
    out = [0.0] * n
    # Mirror slap — bright, percussive.
    for i in range(int(SR * 0.025)):
        if i >= n:
            break
        t = i / SR
        env = math.exp(-t * 220)
        body = math.sin(2 * math.pi * 220 * t) * math.exp(-t * 90)
        nz = (random.random() * 2 - 1)
        out[i] += body * 0.4 + nz * env * 0.7
    # Shutter close — softer, lower second click.
    offset = int(SR * 0.07)
    for i in range(int(SR * 0.03)):
        idx = offset + i
        if idx >= n:
            break
        t = i / SR
        env = math.exp(-t * 160)
        body = math.sin(2 * math.pi * 160 * t) * math.exp(-t * 60)
        nz = (random.random() * 2 - 1)
        out[idx] += body * 0.35 + nz * env * 0.5
    return biquad_highpass(out, 120)


# 9. Polaroid eject — click then motor whirr as film slides out.
def polaroid_eject():
    n = int(SR * 0.34)
    out = [0.0] * n
    # Initial click.
    for i in range(int(SR * 0.015)):
        t = i / SR
        out[i] += (random.random() * 2 - 1) * math.exp(-t * 280) * 0.6
    # Motor whirr — band-passed noise + low tone, starts at click+15ms.
    start = int(SR * 0.02)
    motor_n = n - start
    nz = biquad_lowpass(biquad_highpass(noise(motor_n), 600), 2400)
    for i in range(motor_n):
        t = i / SR
        # Slight amplitude wobble for motor character.
        wobble = 1.0 + 0.4 * math.sin(2 * math.pi * 80 * t)
        tone = math.sin(2 * math.pi * 155 * t) * 0.4
        if t < 0.04:
            env = t / 0.04
        elif t < 0.24:
            env = 1.0
        else:
            env = max(0.0, 1.0 - (t - 0.24) / 0.08)
        out[start + i] += (nz[i] * 0.55 + tone) * env * wobble * 0.6
    return out


# 10. Typewriter clack — sharp keystroke with body resonance, ~70 ms.
def typewriter_clack():
    n = int(SR * 0.09)
    out = []
    nz = noise(n)
    hp = biquad_highpass(nz, 1500)
    for i in range(n):
        t = i / SR
        click_env = math.exp(-t * 320)
        body = math.sin(2 * math.pi * 480 * t) * math.exp(-t * 110)
        body += math.sin(2 * math.pi * 1700 * t) * math.exp(-t * 180) * 0.5
        out.append(hp[i] * click_env * 0.6 + body * 0.45)
    # Tiny carriage tick at the tail.
    tick_at = int(SR * 0.06)
    for i in range(int(SR * 0.012)):
        idx = tick_at + i
        if idx < n:
            t = i / SR
            out[idx] += math.sin(2 * math.pi * 2400 * t) * math.exp(-t * 280) * 0.3
    return out


# 11. Cassette record button — two mechanical clicks engaging, with low thunk.
def cassette_record():
    n = int(SR * 0.16)
    out = [0.0] * n
    # First click — finger-on-button.
    for i in range(int(SR * 0.02)):
        t = i / SR
        out[i] += (random.random() * 2 - 1) * math.exp(-t * 240) * 0.5
        out[i] += math.sin(2 * math.pi * 140 * t) * math.exp(-t * 90) * 0.4
    # Second click — internal mechanism latching, ~35 ms later, deeper.
    offset = int(SR * 0.04)
    for i in range(int(SR * 0.04)):
        idx = offset + i
        if idx >= n:
            break
        t = i / SR
        thunk = math.sin(2 * math.pi * 90 * t) * math.exp(-t * 55)
        thunk += math.sin(2 * math.pi * 220 * t) * math.exp(-t * 90) * 0.3
        nz = (random.random() * 2 - 1) * math.exp(-t * 200)
        out[idx] += thunk * 0.7 + nz * 0.4
    return out


# 12. Xerox flash — the full iconic photocopier vibe: warmup hum, bright
#     sweep with motor whine, terminator clack, finishing beep.
def xerox_flash():
    dur = 0.55
    n = int(SR * dur)
    out = [0.0] * n
    # Warmup hum — 60 Hz mains-style buzz, fades in over 60 ms then rides.
    for i in range(int(SR * 0.32)):
        if i >= n:
            break
        t = i / SR
        hum = math.sin(2 * math.pi * 60 * t)
        hum += math.sin(2 * math.pi * 120 * t) * 0.3  # 2nd harmonic
        if t < 0.06:
            env = t / 0.06
        else:
            env = 1.0 - 0.4 * (t / 0.32)
        out[i] += hum * env * 0.22
    # Mechanical sweep — bandpassed noise with a frequency-swept tone on top,
    # rising in pitch like the lamp sled accelerating across the platen.
    sweep_start = int(SR * 0.05)
    sweep_dur = int(SR * 0.30)
    sweep_nz = biquad_lowpass(biquad_highpass(noise(sweep_dur), 900), 4200)
    for i in range(sweep_dur):
        idx = sweep_start + i
        if idx >= n:
            break
        t = i / SR
        progress = t / (sweep_dur / SR)
        freq = 420 + 880 * progress
        whine = math.sin(2 * math.pi * freq * t)
        if t < 0.025:
            env = t / 0.025
        elif progress < 0.85:
            env = 1.0
        else:
            env = max(0.0, 1.0 - (progress - 0.85) / 0.15)
        out[idx] += (whine * 0.32 + sweep_nz[i] * 0.45) * env
    # Terminator clack — sled hitting end of travel.
    clack_at = int(SR * 0.36)
    for i in range(int(SR * 0.025)):
        idx = clack_at + i
        if idx >= n:
            break
        t = i / SR
        out[idx] += (random.random() * 2 - 1) * math.exp(-t * 200) * 0.7
        out[idx] += math.sin(2 * math.pi * 280 * t) * math.exp(-t * 80) * 0.3
    # Finishing beep — short sine pip, ~80 ms after the clack.
    beep_at = int(SR * 0.44)
    for i in range(int(SR * 0.09)):
        idx = beep_at + i
        if idx >= n:
            break
        t = i / SR
        if t < 0.008:
            env = t / 0.008
        elif t < 0.07:
            env = 1.0
        else:
            env = max(0.0, 1.0 - (t - 0.07) / 0.02)
        out[idx] += math.sin(2 * math.pi * 1320 * t) * env * 0.4
    return biquad_highpass(out, 45)


# 6. Index-card flip — short rasp + soft tap, ~110 ms.
def card_flip():
    n = int(SR * 0.12)
    nz = noise(n)
    bp = biquad_lowpass(biquad_highpass(nz, 1800), 6000)
    out = []
    for i in range(n):
        t = i / SR
        env = max(0.0, 1.0 - t / 0.07) if t < 0.07 else 0.0
        out.append(bp[i] * env * 0.55)
    # Tail tap.
    for i in range(int(SR * 0.025)):
        idx = int(SR * 0.085) + i
        if idx >= n:
            break
        t = i / SR
        out[idx] += math.sin(2 * math.pi * 1400 * t) * math.exp(-t * 250) * 0.5
    return out


def main():
    candidates = {
        "1-pin-tack": pin_tack(),
        "2-paper-grab": paper_grab(),
        "3-scissor-snip": snip(),
        "4-sticky-peel": sticky_peel(),
        "5-stamp-thud": stamp(),
        "6-card-flip": card_flip(),
        "7-photocopy-zip": photocopy_zip(),
        "8-camera-shutter": camera_shutter(),
        "9-polaroid-eject": polaroid_eject(),
        "10-typewriter-clack": typewriter_clack(),
        "11-cassette-record": cassette_record(),
        "12-xerox-flash": xerox_flash(),
    }
    for name, samples in candidates.items():
        path = write_wav(name, samples)
        print(path)


if __name__ == "__main__":
    random.seed(7)  # deterministic so we can iterate on the recipes
    main()
