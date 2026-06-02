"""Hardware-free tests for the mixing core and DSP.

These exercise the numpy signal path directly (no PortAudio / Qt / MIDI), so they
run in CI and headless environments.
"""

import numpy as np
import pytest

from discrod.audio.engine import AudioEngine
from discrod.audio.clip import Clip, MODE_ONESHOT, MODE_GATE, MODE_TOGGLE
from discrod.audio.dsp import Gate, Compressor, Equalizer, db_to_linear


SR = 48000


def make_clip(value=0.5, frames=1000, channels=2):
    samples = np.full((frames, channels), value, dtype=np.float32)
    return Clip("test.wav", samples, SR)


def test_mic_passthrough():
    eng = AudioEngine(SR, block_size=128)
    mic = np.full((128, 2), 0.25, dtype=np.float32)
    out = eng.render_block(128, mic_block=mic)
    assert np.allclose(out, 0.25, atol=1e-6)


def test_clip_mixes_with_mic():
    eng = AudioEngine(SR, block_size=128)
    clip = make_clip(0.5)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_ONESHOT)
    mic = np.full((128, 2), 0.1, dtype=np.float32)
    out = eng.render_block(128, mic_block=mic)
    # mic (0.1 on mic channel) + clip (0.5 on soundboard) summed at master.
    assert np.allclose(out, 0.6, atol=1e-6)


def test_oneshot_finishes_and_is_culled():
    eng = AudioEngine(SR, block_size=600)
    clip = make_clip(0.5, frames=1000)
    eng.trigger_clip(clip, "Soundboard")
    eng.render_block(600)            # consume 600 of 1000
    eng.render_block(600)            # consume remaining 400, voice ends
    assert eng._voices == []


def test_mute_silences_channel():
    eng = AudioEngine(SR, block_size=64)
    eng.soundboard_channel.mute = True
    clip = make_clip(0.9)
    eng.trigger_clip(clip, "Soundboard")
    out = eng.render_block(64, mic_block=np.zeros((64, 2), dtype=np.float32))
    assert np.allclose(out, 0.0)


def test_solo_isolates_channel():
    eng = AudioEngine(SR, block_size=64)
    eng.soundboard_channel.solo = True
    clip = make_clip(0.4)
    eng.trigger_clip(clip, "Soundboard")
    mic = np.full((64, 2), 0.3, dtype=np.float32)
    out = eng.render_block(64, mic_block=mic)
    # Mic excluded because only Soundboard is soloed.
    assert np.allclose(out, 0.4, atol=1e-6)


def test_toggle_mode_starts_and_stops():
    eng = AudioEngine(SR, block_size=64)
    clip = make_clip(0.5, frames=100000)
    key = ("note", 60)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_TOGGLE, key=key)
    assert len([v for v in eng._voices if v.active]) == 1
    eng.trigger_clip(clip, "Soundboard", mode=MODE_TOGGLE, key=key)
    assert len([v for v in eng._voices if v.active]) == 0


def test_gate_note_off_stops_voice():
    eng = AudioEngine(SR, block_size=64)
    clip = make_clip(0.5, frames=100000)
    key = ("note", 62)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_GATE, key=key)
    eng.release_clip(key)
    out = eng.render_block(64)
    assert np.allclose(out, 0.0)


def test_master_gain_and_clipping():
    eng = AudioEngine(SR, block_size=32)
    eng.master_gain_db = 20.0  # large boost
    mic = np.full((32, 2), 0.5, dtype=np.float32)
    out = eng.render_block(32, mic_block=mic)
    assert out.max() <= 1.0 and out.min() >= -1.0


def test_compressor_reduces_loud_signal():
    comp = Compressor(SR, threshold_db=-20.0, ratio=4.0, attack_ms=0.1,
                      release_ms=1.0, enabled=True)
    loud = np.full((4800, 2), db_to_linear(-6.0), dtype=np.float32)
    comp.process(loud)
    # After settling, output should be below the input level.
    assert np.max(np.abs(loud[-100:])) < db_to_linear(-6.0)


def test_gate_closes_on_quiet_signal():
    gate = Gate(SR, threshold_db=-40.0, range_db=-80.0, attack_ms=0.1,
                hold_ms=0.0, release_ms=0.5, enabled=True)
    quiet = np.full((4800, 2), db_to_linear(-60.0), dtype=np.float32)
    gate.process(quiet)
    assert np.max(np.abs(quiet[-100:])) < db_to_linear(-60.0)


def test_eq_flat_when_zero_gain_is_passthrough():
    eq = Equalizer(SR, channels=2, enabled=True)
    sig = np.random.randn(256, 2).astype(np.float32) * 0.1
    original = sig.copy()
    eq.process(sig)  # all bands at 0 dB -> skipped
    assert np.allclose(sig, original)


def test_eq_changes_signal_when_boosted():
    eq = Equalizer(SR, channels=2, enabled=True)
    eq.set_band_gain(1, 12.0)  # boost mid
    sig = np.sin(2 * np.pi * 1000 * np.arange(2048) / SR).astype(np.float32)
    sig = np.column_stack([sig, sig])
    out = sig.copy()
    eq.process(out)
    assert not np.allclose(out, sig)


def test_config_roundtrip():
    eng = AudioEngine(SR)
    eng.master_gain_db = -3.0
    eng.soundboard_channel.eq.enabled = True
    eng.soundboard_channel.eq.set_band_gain(0, 6.0)
    data = eng.to_dict()
    eng2 = AudioEngine(SR)
    eng2.load_dict(data)
    assert eng2.master_gain_db == -3.0
    assert eng2.soundboard_channel.eq.enabled is True
    assert eng2.soundboard_channel.eq.bands[0].gain_db == 6.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
