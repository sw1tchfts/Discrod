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
    assert eng.monitor_ring.available <= eng._monitor_tap.prime_frames


def test_transport_cushions_stay_deep():
    # Both ring consumers race a virtual cable's bursty callback; shallow
    # primes hover at the underrun threshold and click. Guard both cushions:
    # the mic needs real depth (>= 8 blocks), the latency-tolerant monitor
    # deeper still.
    eng = make_engine()
    assert eng._mic_tap.prime_frames >= 8 * eng.block_size
    assert eng._monitor_tap.prime_frames >= 1.5 * eng._mic_tap.prime_frames


def test_underrun_fades_out_and_reprimes():
    eng = make_engine()
    eng._monitor_tap.primed = True  # force primed with a partial block in the ring
    eng.monitor_ring.write(np.ones((100, 2), dtype=np.float32))
    out = eng._read_monitor(BLOCK)
    # Remainder fades from full scale to zero, then true silence: no hard edge.
    assert out[0, 0] == 1.0
    assert out[99, 0] == 0.0
    assert np.all(out[100:] == 0.0)
    assert np.all(np.abs(np.diff(out[:, 0])) < 0.05)
    assert eng._monitor_tap.primed is False


def test_resume_after_prime_fades_in():
    eng = make_engine()
    eng.monitor_ring.write(
        np.ones((eng._monitor_tap.prime_frames, 2), dtype=np.float32))
    block = eng._read_monitor(BLOCK)
    fade = AudioEngine.DECLICK_FADE
    # First sample silent, ramp up, then full scale: no click on resume.
    assert block[0, 0] == 0.0
    assert np.all(block[fade:, 0] == 1.0)
    assert np.all(np.abs(np.diff(block[:, 0])) < 0.05)
    # Steady state afterwards: no fade applied to the next block.
    again = eng._read_monitor(BLOCK)
    assert np.all(again[:, 0] == 1.0)


def test_servo_absorbs_slow_writer():
    # Writer delivers 250 frames per 256-frame read: a sustained ~2.3% clock
    # slew (VB-CABLE-style). The servo must absorb it with zero gaps - with
    # the old fixed-ratio consumer this underran periodically (clicking).
    eng = make_engine()
    eng.monitor_ring.write(
        np.ones((eng._monitor_tap.prime_frames, 2), dtype=np.float32))
    outputs = []
    for _ in range(300):
        eng.monitor_ring.write(np.ones((250, 2), dtype=np.float32))
        outputs.append(eng._read_monitor(BLOCK))
    tail = np.concatenate(outputs[5:])
    assert np.all(tail == 1.0)  # constant in -> constant out, no gaps/fades
    assert eng.monitor_underruns == 0


def test_servo_absorbs_fast_writer():
    # Writer delivers 262 frames per 256-frame read (~2.3% fast): the servo
    # must consume faster instead of accumulating a backlog and dropping.
    eng = make_engine()
    eng.monitor_ring.write(
        np.ones((eng._monitor_tap.prime_frames, 2), dtype=np.float32))
    outputs = []
    for _ in range(300):
        eng.monitor_ring.write(np.ones((262, 2), dtype=np.float32))
        outputs.append(eng._read_monitor(BLOCK))
    tail = np.concatenate(outputs[5:])
    assert np.all(tail == 1.0)
    assert eng.monitor_drops == 0
    assert eng.monitor_ring.available < eng._monitor_tap.max_fill


def test_servo_ratio_stays_clamped():
    # A mismatch beyond the servo's range (here ~22%) degrades to occasional
    # declicked gaps - the ratio must never chase outside its clamp.
    eng = make_engine()
    eng.monitor_ring.write(
        np.ones((eng._monitor_tap.prime_frames, 2), dtype=np.float32))
    for _ in range(300):
        eng.monitor_ring.write(np.ones((200, 2), dtype=np.float32))
        out = eng._read_monitor(BLOCK)
        assert np.all(np.isfinite(out))
        assert np.all(np.abs(out) <= 1.0)
    lo = 1.0 - AudioEngine.SERVO_RANGE
    hi = 1.0 + AudioEngine.SERVO_RANGE
    assert lo <= eng._monitor_tap.ratio <= hi
    assert eng.monitor_underruns > 0  # degradation is counted, not silent


def test_servo_interpolation_preserves_waveform_shape():
    # A linear ramp through the resampler must stay monotonic (interpolation
    # correctness; a phase/index bug would produce jumps or repeats).
    eng = make_engine()
    n = eng._monitor_tap.prime_frames + 4 * BLOCK
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    eng.monitor_ring.write(np.repeat(ramp[:, None], 2, axis=1))
    eng._read_monitor(BLOCK)  # first block carries the prime fade-in
    for _ in range(3):
        block = eng._read_monitor(BLOCK)
        diffs = np.diff(block[:, 0])
        assert np.all(diffs >= 0.0)
        assert np.max(np.abs(diffs)) < 1e-3  # smooth, no repeats/jumps


def test_glitch_counters_reset_and_count():
    eng = make_engine()
    assert eng.monitor_underruns == 0
    eng._monitor_tap.primed = True
    eng.monitor_ring.write(np.ones((100, 2), dtype=np.float32))
    eng._read_monitor(BLOCK)  # underrun
    assert eng.monitor_underruns == 1


def test_mic_servo_absorbs_slow_writer():
    # The live path has the same disease: the mic ring's consumer is the
    # virtual cable's callback. A sustained ~2.3% slew must produce zero
    # mic gaps (this is what "glitches mic N" was counting on real hardware).
    eng = AudioEngine(sample_rate=48000, block_size=BLOCK)
    eng.mic_ring.write(
        np.full((eng._mic_tap.prime_frames, 2), 0.5, dtype=np.float32))
    outputs = []
    for _ in range(300):
        eng.mic_ring.write(np.full((250, 2), 0.5, dtype=np.float32))
        outputs.append(eng.render_block(BLOCK))
    tail = np.concatenate(outputs[5:])
    assert np.all(tail == 0.5)
    assert eng.mic_underruns == 0


def test_mic_servo_absorbs_fast_writer():
    eng = AudioEngine(sample_rate=48000, block_size=BLOCK)
    eng.mic_ring.write(
        np.full((eng._mic_tap.prime_frames, 2), 0.5, dtype=np.float32))
    outputs = []
    for _ in range(300):
        eng.mic_ring.write(np.full((262, 2), 0.5, dtype=np.float32))
        outputs.append(eng.render_block(BLOCK))
    tail = np.concatenate(outputs[5:])
    assert np.all(tail == 0.5)
    assert eng.mic_drops == 0
    assert eng.mic_ring.available < eng._mic_tap.max_fill


def test_tap_bridges_nominal_rate_mismatch_48k_to_44k1():
    # A 48000-rate ring consumed by a 44100-rate callback: base_ratio carries
    # the conversion (~1.0884 input frames per output frame). This is the
    # fallback for devices that reject the engine rate (PaErrorCode -9997).
    from discrod.audio.engine import _ServoTap
    from discrod.audio.ringbuffer import RingBuffer
    base = 48000.0 / 44100.0
    tap = _ServoTap(2, 12 * BLOCK, 36 * BLOCK, 128, 0.05, 0.02, 0.05,
                    base_ratio=base)
    ring = RingBuffer(48000, 2)
    ring.write(np.ones((tap.prime_frames, 2), dtype=np.float32))
    outputs = []
    for _ in range(300):
        ring.write(np.ones((279, 2), dtype=np.float32))  # ~256 * 1.0884
        outputs.append(tap.read(ring, BLOCK))
    tail = np.concatenate(outputs[5:])
    assert np.all(tail == 1.0)
    assert tap.underruns == 0 and tap.drops == 0
    assert base * 0.95 <= tap.ratio <= base * 1.05


def test_tap_bridges_nominal_rate_mismatch_44k1_to_48k():
    # The opposite direction: 44100-rate mic ring consumed by a 48000-rate
    # cable callback (base_ratio ~0.919).
    from discrod.audio.engine import _ServoTap
    from discrod.audio.ringbuffer import RingBuffer
    base = 44100.0 / 48000.0
    tap = _ServoTap(2, 8 * BLOCK, 24 * BLOCK, 128, 0.05, 0.02, 0.05,
                    base_ratio=base)
    ring = RingBuffer(48000, 2)
    ring.write(np.full((tap.prime_frames, 2), 0.5, dtype=np.float32))
    outputs = []
    for _ in range(300):
        ring.write(np.full((235, 2), 0.5, dtype=np.float32))  # ~256 * 0.919
        outputs.append(tap.read(ring, BLOCK))
    tail = np.concatenate(outputs[5:])
    assert np.all(tail == 0.5)
    assert tap.underruns == 0 and tap.drops == 0


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
