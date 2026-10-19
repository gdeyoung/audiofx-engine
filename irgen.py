#!/usr/bin/env python3
"""irgen.py — mono IR generator for audiofx bass/night convolver stages.

Usage: irgen.py <ir_dir> bass|night <0-100>

Pure stdlib (no numpy): builds a linear-phase FIR shelf via the
windowed-sinc method and writes it as a 32-bit PCM mono WAV that the
PipeWire builtin convolver accepts directly.

Design:
  LP(fc)  = Hann-windowed sinc, unit DC gain (sum of taps = 1.0)
  bass    = 1 + (g_b - 1) * LP(180 Hz)      # low-shelf boost, g_b = +0.12 dB/pt
  night   = 1 - (1 - g_n) * LP(3.5 kHz)     # high-shelf cut,  g_n = -0.08 dB/pt
513 taps, symmetric, so phase is linear and the chain stays clean.
"""

import math
import os
import struct
import sys
import wave

RATE = 48000
TAPS = 513
BASS_FC = 180.0        # Hz — shelf corner for bass lift
NIGHT_FC = 3500.0      # Hz — shelf corner for night HF cut
BASS_DB_PER_PT = 0.12  # level 100 -> +12 dB shelf
NIGHT_DB_PER_PT = 0.08 # level 100 -> -8 dB shelf


def db_to_gain(db):
    return 10.0 ** (db / 20.0)


def lowpass_taps(fc):
    """Hann-windowed sinc lowpass, normalized to unit DC gain."""
    n = TAPS
    half = (n - 1) // 2
    wc = 2.0 * math.pi * fc / RATE
    taps = []
    for i in range(n):
        m = i - half  # centered
        if m == 0:
            s = wc / math.pi
        else:
            s = math.sin(wc * m) / (math.pi * m)
        # Hann window
        w = 0.5 * (1.0 - math.cos(2.0 * math.pi * i / (n - 1)))
        taps.append(s * w)
    total = sum(taps)
    return [t / total for t in taps]  # DC gain exactly 1.0


def shelf_ir(fc, gain_db, mode):
    """boost: delta + (g-1)*LP  -> DC gain g, HF unity (low shelf up)
    cut:    g*delta + (1-g)*LP -> DC unity, HF gain g (high shelf down)"""
    lp = lowpass_taps(fc)
    g = db_to_gain(gain_db)
    if mode == "boost":
        ir = [(g - 1.0) * t for t in lp]
        ir[(len(lp) - 1) // 2] += 1.0
    else:
        ir = [(1.0 - g) * t for t in lp]
        ir[(len(lp) - 1) // 2] += g
    return ir


def write_wav(path, ir):
    peak = max(abs(x) for x in ir) or 1.0
    scale = (2 ** 31 - 1) / max(1.0, peak)  # never clip; true unity preserved
    frames = b"".join(
        struct.pack("<i", int(round(x * scale))) for x in ir
    )
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(4)  # 32-bit PCM
        w.setframerate(RATE)
        w.writeframes(frames)


HP_FC = 120.0           # Hz — loud stage high-pass corner
HP_DC_GAIN_DB = -30.0   # attenuation at DC; frees the amps' excursion budget
                        # below what these drivers can reproduce (upstream
                        # GalaxyBook EasyEffects presets do the same)


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: irgen.py <ir_dir> bass|night <0-100> | irgen.py <ir_dir> hp")
    ir_dir, kind = sys.argv[1], sys.argv[2]

    if kind == "hp":
        # "boost" mode with g < 1 is a high-shelf down = high-pass: DC gain g,
        # HF unity. Fixed shape; no strength parameter.
        ir = shelf_ir(HP_FC, HP_DC_GAIN_DB, "boost")
        os.makedirs(ir_dir, exist_ok=True)
        out = os.path.join(ir_dir, "hp.wav")
        write_wav(out, ir)
        print("hp: high-pass fc=%.0f g=%.2f -> %s (%d taps, peak=%.3f)"
              % (HP_FC, db_to_gain(HP_DC_GAIN_DB), out, TAPS, max(abs(x) for x in ir)))
        return

    if kind not in ("bass", "night") or len(sys.argv) != 4 or not 0 < int(sys.argv[3]) <= 100:
        sys.exit("kind must be bass|night with level 1..100, or hp")
    level = int(sys.argv[3])

    if kind == "bass":
        ir = shelf_ir(BASS_FC, level * BASS_DB_PER_PT, "boost")
    else:
        ir = shelf_ir(NIGHT_FC, -level * NIGHT_DB_PER_PT, "cut")

    os.makedirs(ir_dir, exist_ok=True)
    out = os.path.join(ir_dir, "%s.wav" % kind)
    write_wav(out, ir)
    print("%s: %s level=%d -> %s (%d taps, peak=%.3f)"
          % (kind, "shelf", level, out, TAPS, max(abs(x) for x in ir)))


if __name__ == "__main__":
    main()
