"""Tests for the local monitor bus (soundboard monitoring + mic-FX listen).

All hardware-free: monitor_enabled is flipped directly and blocks are pulled
through render_block / _read_monitor, mirroring how the live callbacks use
them.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discrod.audio.engine import AudioEngine  # noqa: E402
from discrod.audio.clip import Clip, MODE_ONESHOT  # noqa: E402
from discrod.config import AppConfig  # noqa: E402

BLOCK = 256


def make_engine(**kw):
    eng = AudioEngine(sample_rate=48000, block_size=BLOCK, **kw)
    eng.monitor_enabled = True
    return eng


def make_clip(value=0.25, frames=48000):
    data = np.full((frames, 2), value, dtype=np.float32)
    return Clip("tone.wav", data, 48000)


def drain_monitor(eng, frames):
    """Read the raw monitor ring (bypassing priming) for content checks."""
    return eng.monitor_ring.read(frames)


def test_monitor_carries_soundboard_at_master_level():
    eng = make_engine()
    eng.trigger_clip(make_clip(0.25), "Soundboard", MODE_ONESHOT)
    mic = np.zeros((BLOCK, 2), dtype=np.float32)
    out = eng.render_block(BLOCK, mic_block=mic)
    mon = drain_monitor(eng, BLOCK)
    # Mic silent -> master and monitor are the same signal, sample for sample.
    np.testing.assert_array_equal(mon, out)
    assert np.max(np.abs(mon)) > 0.0


def test_monitor_excludes_mic_by_default():
    eng = make_engine()
    eng.trigger_clip(make_clip(0.25), "Soundboard", MODE_ONESHOT)
    mic = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    out = eng.render_block(BLOCK, mic_block=mic)
    mon = drain_monitor(eng, BLOCK)
    # Master includes the mic; the monitor must not.
    assert not np.array_equal(mon, out)
    # The monitor equals what the same engine state renders with a silent mic.
    eng2 = make_engine()
    eng2.trigger_clip(make_clip(0.25), "Soundboard", MODE_ONESHOT)
    out2 = eng2.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    np.testing.assert_array_equal(mon, out2)


def test_monitor_includes_processed_mic_when_toggled():
    eng = make_engine()
    eng.monitor_mic = True
    mic = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    out = eng.render_block(BLOCK, mic_block=mic)
    mon = drain_monitor(eng, BLOCK)
    # With the toggle on and only the mic sounding, monitor == master:
    # the mic block passes through the same channel FX/gain path.
    np.testing.assert_array_equal(mon, out)
    assert np.max(np.abs(mon)) > 0.0


def test_monitor_toggle_flips_live():
    eng = make_engine()
    mic = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    eng.render_block(BLOCK, mic_block=mic)
    silent = drain_monitor(eng, BLOCK)
    assert np.max(np.abs(silent)) == 0.0
    eng.monitor_mic = True
    eng.render_block(BLOCK, mic_block=mic)
    heard = drain_monitor(eng, BLOCK)
    assert np.max(np.abs(heard)) > 0.0


def test_monitor_tracks_master_gain():
    eng = make_engine()
    eng.master_gain_db = -12.0
    eng.trigger_clip(make_clip(0.5), "Soundboard", MODE_ONESHOT)
    out = eng.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    mon = drain_monitor(eng, BLOCK)
    np.testing.assert_array_equal(mon, out)


def test_monitor_respects_mute_and_solo():
    eng = make_engine()
    eng.trigger_clip(make_clip(0.25), "Soundboard", MODE_ONESHOT)
    eng.soundboard_channel.mute = True
    eng.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    mon = drain_monitor(eng, BLOCK)
    assert np.max(np.abs(mon)) == 0.0


def test_monitor_ring_not_written_when_disabled():
    eng = make_engine()
    eng.monitor_enabled = False
    eng.trigger_clip(make_clip(0.25), "Soundboard", MODE_ONESHOT)
    eng.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    assert eng.monitor_ring.available == 0


def test_read_monitor_primes_before_playing():
    eng = make_engine()
    eng.trigger_clip(make_clip(0.25), "Soundboard", MODE_ONESHOT)
    # One block in the ring is below the prime level -> silence out.
    eng.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    first = eng._read_monitor(BLOCK)
    assert np.max(np.abs(first)) == 0.0
    # Fill to the prime level -> audio flows.
    for _ in range(AudioEngine.MONITOR_PRIME_BLOCKS):
        eng.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    primed = eng._read_monitor(BLOCK)
    assert np.max(np.abs(primed)) > 0.0


def test_read_monitor_drops_backlog_after_drift():
    eng = make_engine()
    eng.trigger_clip(make_clip(0.25, frames=48000 * 4), "Soundboard",
                     MODE_ONESHOT)
    overfill = (AudioEngine.MONITOR_PRIME_BLOCKS
                * AudioEngine.MAX_FILL_FACTOR + 4)
    for _ in range(overfill):
        eng.render_block(BLOCK, mic_block=np.zeros((BLOCK, 2), np.float32))
    eng._read_monitor(BLOCK)
    # Backlog was clamped back to the prime level (one block since consumed).
    assert eng.monitor_ring.available <= eng._monitor_prime_frames


def test_monitor_prime_is_deeper_than_mic_prime():
    # The monitor writer (virtual-cable callback) is bursty; a shallow prime
    # hovers at the underrun threshold and clicks. Guard the cushion.
    eng = make_engine()
    assert eng._monitor_prime_frames >= 3 * eng._prime_frames


def test_underrun_fades_out_and_reprimes():
    eng = make_engine()
    eng._monitor_primed = True  # force primed with a partial block in the ring
    eng.monitor_ring.write(np.ones((100, 2), dtype=np.float32))
    out = eng._read_monitor(BLOCK)
    # Remainder fades from full scale to zero, then true silence: no hard edge.
    assert out[0, 0] == 1.0
    assert out[99, 0] == 0.0
    assert np.all(out[100:] == 0.0)
    assert np.all(np.abs(np.diff(out[:, 0])) < 0.05)
    assert eng._monitor_primed is False


def test_resume_after_prime_fades_in():
    eng = make_engine()
    eng.monitor_ring.write(
        np.ones((eng._monitor_prime_frames, 2), dtype=np.float32))
    block = eng._read_monitor(BLOCK)
    fade = AudioEngine.MONITOR_FADE
    # First sample silent, ramp up, then full scale: no click on resume.
    assert block[0, 0] == 0.0
    assert np.all(block[fade:, 0] == 1.0)
    assert np.all(np.abs(np.diff(block[:, 0])) < 0.05)
    # Steady state afterwards: no fade applied to the next block.
    again = eng._read_monitor(BLOCK)
    assert np.all(again[:, 0] == 1.0)


def test_config_roundtrip_monitor_mic():
    cfg = AppConfig(monitor_mic=True, monitor_device={"name": "Phones",
                                                      "hostapi": "WASAPI"})
    data = cfg.to_dict()
    back = AppConfig.from_dict(data)
    assert back.monitor_mic is True
    assert back.monitor_device == {"name": "Phones", "hostapi": "WASAPI"}
    # Older configs without the key default to off.
    del data["monitor_mic"]
    assert AppConfig.from_dict(data).monitor_mic is False
