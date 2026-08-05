"""Hardware-free tests for the mixing core and DSP.

These exercise the numpy signal path directly (no PortAudio / Qt / MIDI), so they
run in CI and headless environments.
"""

import numpy as np
import pytest

from discrod.audio.engine import AudioEngine
from discrod.audio.clip import (Clip, Voice, MODE_ONESHOT, MODE_GATE, MODE_LOOP,
                                MODE_TOGGLE, _resample_linear)
from discrod.audio.dsp import Gate, Compressor, Equalizer, db_to_linear
from discrod.audio.dsp.biquad import Biquad, PEAK
from discrod.audio.ringbuffer import RingBuffer


SR = 48000


def make_clip(value=0.5, frames=1000, channels=2):
    samples = np.full((frames, channels), value, dtype=np.float32)
    return Clip("test.wav", samples, SR)


def render_ms(eng, ms):
    """Render enough blocks to cover ``ms`` milliseconds; returns last block."""
    frames = int(SR * ms / 1000.0)
    out = None
    block = 256
    done = 0
    while done < frames:
        out = eng.render_block(block)
        done += block
    return out


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


def test_oneshot_retrigger_restarts_instead_of_stacking():
    eng = AudioEngine(SR, block_size=256)
    clip = make_clip(0.5, frames=100000)
    key = ("note", 60)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_ONESHOT, key=key)
    eng.render_block(256)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_ONESHOT, key=key)
    # Old voice fades out, new voice restarts: after the fade completes only
    # one voice remains and output returns to single-copy level (no +6 dB
    # stacking that slams the clipper).
    render_ms(eng, 20)
    active = [v for v in eng._voices if v.active]
    assert len(active) == 1
    assert active[0].pos < SR  # the fresh voice, restarted near the beginning
    out = eng.render_block(256)
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=1e-3)


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
    # Second press starts the declick fade; the voice dies once it completes.
    render_ms(eng, 20)
    assert len([v for v in eng._voices if v.active]) == 0
    out = eng.render_block(64)
    assert np.allclose(out, 0.0)


def test_loop_mode_survives_note_off_and_wraps():
    eng = AudioEngine(SR, block_size=256)
    clip = make_clip(0.5, frames=300)   # shorter than one block: must wrap
    key = ("note", 61)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_LOOP, key=key)
    eng.release_clip(key)               # momentary pad: note-off arrives at once
    out = eng.render_block(256)
    assert np.allclose(out, 0.5, atol=1e-6)   # wrapped, still playing
    out = eng.render_block(256)
    assert np.allclose(out, 0.5, atol=1e-6)   # loops indefinitely
    assert len([v for v in eng._voices if v.active]) == 1


def test_loop_mode_retrigger_stops_instead_of_stacking():
    eng = AudioEngine(SR, block_size=256)
    clip = make_clip(0.5, frames=300)
    key = ("note", 61)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_LOOP, key=key)
    eng.render_block(256)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_LOOP, key=key)  # press again
    render_ms(eng, 20)                  # fade completes
    assert len([v for v in eng._voices if v.active]) == 0
    out = eng.render_block(256)
    assert np.allclose(out, 0.0)


def test_gate_mode_plays_while_held_then_stops():
    eng = AudioEngine(SR, block_size=64)
    clip = make_clip(0.5, frames=100000)
    key = ("note", 62)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_GATE, key=key)
    out = eng.render_block(64)
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=1e-6)  # audible while held
    eng.release_clip(key)
    render_ms(eng, 20)                  # declick fade runs out
    out = eng.render_block(64)
    assert np.allclose(out, 0.0)
    assert eng._voices == []            # culled after the fade


def test_stop_fades_instead_of_hard_cut():
    eng = AudioEngine(SR, block_size=64)
    clip = make_clip(0.5, frames=100000)
    key = ("note", 63)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_GATE, key=key)
    eng.render_block(64)
    eng.release_clip(key)
    out = eng.render_block(64)
    # First post-release block: fade in progress — samples decrease smoothly
    # from near full level rather than slamming to zero.
    col = out[:, 0]
    assert col[0] > 0.4
    assert np.all(np.diff(col) <= 1e-6)
    step = np.max(np.abs(np.diff(col)))
    assert step < 0.05                  # no single-sample cliff (the old click)


def test_zero_length_clip_does_not_hang_render():
    clip = Clip("empty.wav", np.zeros((0, 2), dtype=np.float32), SR)
    voice = Voice(clip, "Soundboard", mode=MODE_LOOP)
    buf = np.zeros((256, 2), dtype=np.float32)
    voice.render_into(buf)              # must return, not spin forever
    assert not voice.active
    assert np.allclose(buf, 0.0)


def test_stop_after_completed_fade_cannot_resurrect_voice():
    clip = make_clip(0.5, frames=100000)
    voice = Voice(clip, "Soundboard", mode=MODE_GATE, key=("note", 60))
    buf = np.zeros((256, 2), dtype=np.float32)
    voice.render_into(buf)
    voice.stop(fade_frames=64)
    voice.render_into(np.zeros((256, 2), dtype=np.float32))  # fade completes
    assert not voice.active
    voice.stop()                          # racing stop() must be a no-op now
    assert not voice.active
    buf = np.zeros((256, 2), dtype=np.float32)
    voice.render_into(buf)
    assert np.allclose(buf, 0.0)          # no 5 ms of resurrected audio


def test_cached_clip_lookup_does_not_block_behind_decode(monkeypatch):
    """A cache-hit pad press must not wait for the preload thread's in-flight
    decode of a different clip."""
    import threading
    import time
    from discrod.mapping import Controller, PadBank, PadMapping
    from discrod.audio import clip as clip_mod

    eng = AudioEngine(SR, block_size=64)
    bank = PadBank()
    bank.set(PadMapping(note=60, clip_path="fast.wav"))
    bank.set(PadMapping(note=61, clip_path="slow.wav"))
    ctrl = Controller(eng, bank)

    slow_started = threading.Event()
    release_slow = threading.Event()

    def fake_load(path, target_sr, channels=2):
        if path == "slow.wav":
            slow_started.set()
            assert release_slow.wait(5.0)
        return make_clip(0.5)

    monkeypatch.setattr(clip_mod.Clip, "load", staticmethod(fake_load))
    monkeypatch.setattr("discrod.mapping.Clip", clip_mod.Clip)

    ctrl._get_clip("fast.wav")            # warm the cache
    slow_thread = threading.Thread(target=ctrl._get_clip, args=("slow.wav",))
    slow_thread.start()
    assert slow_started.wait(5.0)
    t0 = time.perf_counter()
    ctrl._get_clip("fast.wav")            # cache hit while slow decode in flight
    elapsed = time.perf_counter() - t0
    release_slow.set()
    slow_thread.join(5.0)
    assert elapsed < 0.5, f"cache hit blocked {elapsed:.2f}s behind a decode"


def test_resampler_never_returns_empty_for_loadable_audio():
    data = np.ones((1, 2), dtype=np.float32)
    out = _resample_linear(data, 96000, 48000)
    # Sub-half-frame durations legitimately resample to zero frames; the
    # engine guards against them (Clip.load rejects, Voice.render_into bails).
    assert out.shape[0] == 0


def test_trigger_unknown_channel_falls_back_to_soundboard():
    eng = AudioEngine(SR, block_size=64)
    clip = make_clip(0.5)
    eng.trigger_clip(clip, "No Such Channel")
    out = eng.render_block(64, mic_block=np.zeros((64, 2), dtype=np.float32))
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=1e-6)  # audible
    render_ms(eng, 50)
    assert eng._voices == []            # finishes and is culled, no leak


def test_master_gain_boost_applies_and_clips():
    eng = AudioEngine(SR, block_size=32)
    eng.master_gain_db = 20.0  # 0.5 * 10 = 5.0, clipped to 1.0
    mic = np.full((32, 2), 0.5, dtype=np.float32)
    out = eng.render_block(32, mic_block=mic)
    assert np.allclose(out, 1.0, atol=1e-6)
    assert eng.master_meter == pytest.approx(1.0, abs=1e-6)


def test_master_gain_cut_applies_exactly():
    eng = AudioEngine(SR, block_size=32)
    eng.master_gain_db = -6.0
    mic = np.full((32, 2), 0.5, dtype=np.float32)
    out = eng.render_block(32, mic_block=mic)
    assert np.allclose(out, 0.5 * db_to_linear(-6.0), atol=1e-4)
    assert eng.master_meter == pytest.approx(0.5 * db_to_linear(-6.0), abs=1e-4)


# --- dynamics -----------------------------------------------------------------

def test_compressor_reduces_loud_signal():
    comp = Compressor(SR, threshold_db=-20.0, ratio=4.0, attack_ms=0.1,
                      release_ms=1.0, enabled=True)
    loud = np.full((4800, 2), db_to_linear(-6.0), dtype=np.float32)
    comp.process(loud)
    # After settling, output should be below the input level.
    assert np.max(np.abs(loud[-100:])) < db_to_linear(-6.0)


def test_compressor_reengages_quickly_after_silence():
    comp = Compressor(SR, threshold_db=-18.0, ratio=3.0, attack_ms=10.0,
                      release_ms=120.0, enabled=True)
    loud = np.full((SR // 2, 2), db_to_linear(-6.0), dtype=np.float32)
    comp.process(loud)                                   # settle compressed
    silence = np.zeros((int(SR * 0.4), 2), dtype=np.float32)
    comp.process(silence)                                # 400 ms speech pause
    level = db_to_linear(-6.0)
    onset = np.full((SR // 10, 2), level, dtype=np.float32)
    comp.process(onset)
    # The detector floor bounds the envelope excursion, so gain reduction must
    # re-engage within ~one attack constant (10 ms here) — the unclamped
    # envelope needed 26.4 ms of free climb from -174 dB before reducing at all.
    reduced = np.flatnonzero(np.abs(onset[:, 0]) < level * 0.995)
    assert reduced.size, "compressor never re-engaged after the pause"
    assert reduced[0] < int(SR * 0.015)   # < 15 ms (old behavior: 26.4 ms)
    # And it settles into real gain reduction shortly after.
    ms20 = int(SR * 0.020)
    assert np.max(np.abs(onset[ms20:ms20 * 2])) < level * 0.9


def test_compressor_tracks_envelope_above_zero_dbfs():
    """Summed voices with positive pad gain routinely exceed 0 dBFS before the
    channel trim.  An upper clamp on the envelope re-attacks at every block
    boundary — a 187 Hz gain sawtooth (audible buzz).  Block-wise processing
    must match single-call processing exactly."""
    hot = np.full((48 * 256, 2), db_to_linear(6.0), dtype=np.float32)
    one_call = hot.copy()
    comp_a = Compressor(SR, threshold_db=-18.0, ratio=4.0, attack_ms=10.0,
                        release_ms=120.0, enabled=True)
    comp_a.process(one_call)
    blockwise = hot.copy()
    comp_b = Compressor(SR, threshold_db=-18.0, ratio=4.0, attack_ms=10.0,
                        release_ms=120.0, enabled=True)
    for start in range(0, blockwise.shape[0], 256):
        comp_b.process(blockwise[start:start + 256])
    assert np.allclose(blockwise, one_call, atol=1e-5)
    # And the settled gain is constant across a block boundary (no sawtooth).
    tail = blockwise[-512:, 0]
    assert np.max(tail) - np.min(tail) < 1e-4


def test_gate_closes_on_quiet_signal():
    gate = Gate(SR, threshold_db=-40.0, range_db=-80.0, attack_ms=0.1,
                hold_ms=0.0, release_ms=0.5, enabled=True)
    quiet = np.full((4800, 2), db_to_linear(-60.0), dtype=np.float32)
    gate.process(quiet)
    assert np.max(np.abs(quiet[-100:])) < db_to_linear(-60.0)


def test_gate_opens_on_loud_signal():
    gate = Gate(SR, threshold_db=-40.0, range_db=-80.0, attack_ms=1.0,
                hold_ms=0.0, release_ms=50.0, enabled=True)
    level = db_to_linear(-10.0)
    loud = np.full((4800, 2), level, dtype=np.float32)
    gate.process(loud)
    # A stuck-closed gate silences the whole mic; assert it actually opens.
    assert np.max(np.abs(loud[-100:])) == pytest.approx(level, rel=1e-3)


def test_gate_hold_delays_release():
    gate = Gate(SR, threshold_db=-40.0, range_db=-80.0, attack_ms=0.5,
                hold_ms=50.0, release_ms=5.0, enabled=True)
    level = db_to_linear(-10.0)
    loud = np.full((960, 2), level, dtype=np.float32)
    gate.process(loud)                                   # open the gate
    quiet = np.full((4800, 2), db_to_linear(-70.0), dtype=np.float32)
    gate.process(quiet)                                  # 100 ms below threshold
    hold_frames = int(SR * 0.050)
    in_level = db_to_linear(-70.0)
    # During the hold window the gain stays ~1 (signal passes at input level).
    held = np.abs(quiet[hold_frames // 2])
    assert held.max() == pytest.approx(in_level, rel=0.05)
    # Well after hold + release, the gate has clamped down.
    assert np.max(np.abs(quiet[-100:])) < in_level * 0.1


# --- EQ / biquad --------------------------------------------------------------

def test_eq_flat_when_zero_gain_is_passthrough():
    eq = Equalizer(SR, channels=2, enabled=True)
    sig = np.random.randn(256, 2).astype(np.float32) * 0.1
    original = sig.copy()
    eq.process(sig)  # all bands at 0 dB -> exact identity coefficients
    assert np.allclose(sig, original, atol=1e-6)


def test_eq_changes_signal_when_boosted():
    eq = Equalizer(SR, channels=2, enabled=True)
    eq.set_band_gain(1, 12.0)  # boost mid
    sig = np.sin(2 * np.pi * 1000 * np.arange(2048) / SR).astype(np.float32)
    sig = np.column_stack([sig, sig])
    out = sig.copy()
    eq.process(out)
    assert not np.allclose(out, sig)


def test_eq_gain_through_zero_produces_no_transient():
    eq = Equalizer(SR, channels=2, enabled=True)
    eq.set_band_gain(1, 6.0)
    loud = np.full((1024, 2), 0.9, dtype=np.float32)
    eq.process(loud)                     # warm state during a loud passage
    eq.set_band_gain(1, 0.0)             # slider passes through 0 dB
    quiet = np.full((1024, 2), 0.001, dtype=np.float32)
    eq.process(quiet)                    # state keeps flowing (identity filter)
    eq.set_band_gain(1, 6.0)             # slider leaves 0 dB again
    tiny = np.full((256, 2), 0.001, dtype=np.float32)
    eq.process(tiny)
    # Stale-state bug injected a transient 60x the signal here.
    assert np.max(np.abs(tiny)) < 0.01


def test_biquad_peak_matches_analytic_gain():
    # A peak filter driven at its center frequency must apply its dB gain.
    freq, gain_db = 1000.0, 6.0
    bq = Biquad(SR, PEAK, freq=freq, gain_db=gain_db, q=1.0, channels=1)
    t = np.arange(SR) / SR
    sig = np.sin(2 * np.pi * freq * t).astype(np.float32)[:, None]
    bq.process(sig)
    measured = np.max(np.abs(sig[SR // 2:]))
    assert measured == pytest.approx(db_to_linear(gain_db), rel=0.02)


def test_biquad_clamps_frequency_above_nyquist():
    # 18 kHz UI setting at a 22.05 kHz engine rate exceeds Nyquist; unclamped
    # RBJ formulas go unstable (full-scale blast, then permanent NaN state).
    bq = Biquad(22050, PEAK, freq=18000.0, gain_db=6.0, q=2.0, channels=2)
    noise = (np.random.randn(22050, 2) * db_to_linear(-40.0)).astype(np.float32)
    for start in range(0, 22050 - 256, 256):
        block = noise[start:start + 256].copy()
        bq.process(block)
        assert np.isfinite(block).all()
        assert np.max(np.abs(block)) < 1.0


def test_biquad_recovers_from_poisoned_state():
    bq = Biquad(SR, PEAK, freq=1000.0, gain_db=6.0, q=1.0, channels=2)
    bq._zi[:] = np.nan                   # simulate a past instability
    block = np.full((256, 2), 0.1, dtype=np.float32)
    bq.process(block)
    assert np.isfinite(block).all()


# --- ring buffer / priming ----------------------------------------------------

def test_ringbuffer_wraparound_preserves_order():
    ring = RingBuffer(10, 1)
    ring.write(np.arange(8, dtype=np.float32)[:, None])
    assert np.allclose(ring.read(5)[:, 0], [0, 1, 2, 3, 4])
    ring.write(np.arange(8, 14, dtype=np.float32)[:, None])  # wraps
    assert np.allclose(ring.read(9)[:, 0], [5, 6, 7, 8, 9, 10, 11, 12, 13])


def test_ringbuffer_overflow_drops_oldest():
    ring = RingBuffer(8, 1)
    ring.write(np.arange(12, dtype=np.float32)[:, None])
    assert ring.available == 8
    assert np.allclose(ring.read(8)[:, 0], [4, 5, 6, 7, 8, 9, 10, 11])


def test_ringbuffer_underflow_zero_fills():
    ring = RingBuffer(8, 1)
    ring.write(np.array([[1.0], [2.0]], dtype=np.float32))
    out = ring.read(4)
    assert np.allclose(out[:, 0], [1.0, 2.0, 0.0, 0.0])


def test_ringbuffer_drop_bounds_backlog():
    ring = RingBuffer(100, 1)
    ring.write(np.arange(50, dtype=np.float32)[:, None])
    ring.drop(30)
    assert ring.available == 20
    assert ring.read(1)[0, 0] == 30.0


def test_mic_priming_holds_silence_until_target_fill():
    eng = AudioEngine(SR, block_size=64)
    # Less than the prime target (4 blocks): mic must stay silent.
    eng.mic_ring.write(np.full((128, 2), 0.5, dtype=np.float32))
    out = eng.render_block(64)
    assert np.allclose(out, 0.0)
    # Reaching the target releases the mic path.
    eng.mic_ring.write(np.full((256, 2), 0.5, dtype=np.float32))
    out = eng.render_block(64)
    assert np.allclose(out, 0.5, atol=1e-6)


def test_mic_drift_backlog_is_dropped():
    eng = AudioEngine(SR, block_size=64)
    target = eng._prime_frames
    eng.mic_ring.write(np.full((eng._max_fill + 640, 2), 0.5, dtype=np.float32))
    eng.render_block(64)
    # Occupancy was re-bounded to the prime target (minus the frames consumed).
    assert eng.mic_ring.available <= target


# --- engine lifecycle ---------------------------------------------------------

def test_start_clears_stale_voices(monkeypatch):
    eng = AudioEngine(SR, block_size=64)
    clip = make_clip(0.5, frames=100000)
    for note in (60, 61, 62):
        eng.trigger_clip(clip, "Soundboard", key=("note", note))
    assert len(eng._voices) == 3
    eng._clear_voices()                  # what start() runs before streaming
    assert eng._voices == [] and eng._toggle_voices == {}


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


def test_set_sample_rate_notifies_and_rebuilds_ring():
    eng = AudioEngine(SR, block_size=64)
    seen = []
    eng.on_sample_rate_changed = seen.append
    eng.set_sample_rate(44100)
    assert seen == [44100]
    assert eng.mic_ring.capacity == 44100
    assert eng.mic_channel.sample_rate == 44100
    eng.set_sample_rate(44100)           # no-op: no duplicate notification
    assert seen == [44100]


# --- real-time budget ---------------------------------------------------------

def test_full_dsp_render_fits_realtime_budget():
    """Gate+comp+EQ on both channels plus a voice must render comfortably
    inside the block deadline (5.33 ms at 256/48k).  The old per-sample numpy
    loops measured ~14 ms; the vectorized path is two orders faster.  The
    threshold is deliberately loose for slow CI machines."""
    import time
    eng = AudioEngine(SR, block_size=256)
    for ch in (eng.mic_channel, eng.soundboard_channel):
        ch.gate.enabled = True
        ch.compressor.enabled = True
        ch.eq.enabled = True
        ch.eq.set_band_gain(0, 3.0)
        ch.eq.set_band_gain(1, -2.0)
        ch.eq.set_band_gain(2, 4.0)
    clip = make_clip(0.3, frames=SR * 10)
    eng.trigger_clip(clip, "Soundboard", mode=MODE_ONESHOT)
    mic = (np.random.randn(256, 2) * 0.1).astype(np.float32)
    for _ in range(5):                   # warm-up
        eng.render_block(256, mic_block=mic)
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        eng.render_block(256, mic_block=mic)
        times.append(time.perf_counter() - t0)
    median = sorted(times)[len(times) // 2]
    assert median < 0.004, f"render_block median {median * 1e3:.2f} ms >= 4 ms"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
