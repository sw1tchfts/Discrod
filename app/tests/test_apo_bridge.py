"""Tests for the driver-free APO transport bridge (app/discrod/audio/apo_bridge).

Three layers:
  * pure-Python: struct layout matches the C++ protocol, ring round-trips,
    parameter seqlock encodes engine state correctly;
  * DSP parity: the same input through the Python processors and the C++
    ``dsp_oracle`` (built from apo/) agrees within a tight tolerance;
  * end-to-end: audio written by the Python bridge is read back and mixed by
    the C++ ``bridge_probe`` through a real memory mapping.

The C++ layers are skipped automatically when the apo/ binaries have not been
built (so the suite still passes in a Python-only checkout), and covered
directly by apo/tests on platforms where they are built.
"""

import os
import struct
import subprocess

import numpy as np
import pytest

from discrod.audio.engine import AudioEngine
from discrod.audio.dsp.biquad import PEAK, LOW_SHELF, HIGH_SHELF, LOW_PASS
from discrod.audio import apo_bridge as B


SR = 48000

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APO_BUILD = os.path.join(_REPO, "apo", "build")


def _tool(name):
    for cand in (os.path.join(_APO_BUILD, name),
                 os.path.join(_APO_BUILD, name + ".exe")):
        if os.path.exists(cand):
            return cand
    return None


@pytest.fixture
def bridge_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCROD_BRIDGE_DIR", str(tmp_path))
    return B.bridge_path()


# --- layout -----------------------------------------------------------------

def test_params_size_matches_protocol():
    # 16 scalar u32/f32 fields + 3 EQ bands * 16 bytes = 112 (see the C++
    # static_assert in bridge_protocol.h).
    assert B.PARAMS_SIZE == 4 * 16 + 16 * B.EQ_BANDS == 112


def test_file_size_and_offsets():
    assert B.RING_OFFSET == 4096
    assert B.BRIDGE_FILE_SIZE == 4096 + B.RING_CAPACITY_FRAMES * B.RING_MAX_CHANNELS * 4
    assert B.PARAMS_OFFSET + B.PARAMS_SIZE < B.RING_OFFSET


# --- ring round-trip --------------------------------------------------------

def test_open_initializes_header(bridge_path):
    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        assert br._get_u32(B._OFF_MAGIC) == B.BRIDGE_MAGIC
        assert br._get_u32(B._OFF_VERSION) == B.BRIDGE_VERSION
        assert br._get_u32(B._OFF_RING_SR) == SR
        assert br._get_u32(B._OFF_RING_CH) == 2
        assert br._get_u32(B._OFF_RING_CAP) == B.RING_CAPACITY_FRAMES
        assert br.write_pos == 0


def test_write_soundboard_advances_and_lands_in_ring(bridge_path):
    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        block = np.full((256, 2), 0.3, dtype=np.float32)
        n = br.write_soundboard(block)
        assert n == 256
        assert br.write_pos == 256
        assert br._get_u32(B._OFF_WRITE_POS) == 256
        # Read the raw ring region back and confirm the samples are there.
        assert np.allclose(br._ring[:256], 0.3, atol=1e-6)
        assert np.allclose(br._ring[256:512], 0.0)


def test_ring_wraps(bridge_path):
    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        cap = B.RING_CAPACITY_FRAMES
        # Advance write_pos to near the end, then write across the boundary.
        br._write_pos = cap - 100
        block = np.arange(200 * 2, dtype=np.float32).reshape(200, 2) / 1000.0
        br.write_soundboard(block)
        assert br.write_pos == (cap + 100) & 0xFFFFFFFF
        assert np.allclose(br._ring[cap - 100:cap], block[:100], atol=1e-6)
        assert np.allclose(br._ring[0:100], block[100:], atol=1e-6)


def test_ring_fill_tracks_reader(bridge_path):
    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        br.write_soundboard(np.zeros((512, 2), dtype=np.float32))
        assert br.ring_fill() == 512
        # Simulate the APO consuming 200 frames.
        br._put_u32(B._OFF_READ_POS, 200)
        assert br.ring_fill() == 312


# --- parameter seqlock ------------------------------------------------------

def _unpack_params(br):
    blob = bytes(br._mm[B.PARAMS_OFFSET:B.PARAMS_OFFSET + B.PARAMS_SIZE])
    return struct.unpack(B._PARAMS_FORMAT, blob)


def test_publish_params_encodes_engine_state(bridge_path):
    eng = AudioEngine(sample_rate=SR, block_size=256, channels=2)
    mic = eng.mic_channel
    mic.gain_db = -3.0
    mic.gate.enabled = True
    mic.gate.threshold_db = -40.0
    mic.compressor.enabled = True
    mic.compressor.ratio = 4.0
    mic.eq.enabled = True
    mic.eq.set_band_gain(1, 6.0)  # mid peak +6 dB

    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        br.publish_params(eng)
        seq = br._get_u32(B._OFF_PARAM_SEQ)
        assert seq % 2 == 0 and seq > 0  # even => consistent

        vals = _unpack_params(br)
        assert vals[0] == pytest.approx(-3.0)          # mic_gain_db
        assert vals[1] == 1                            # gate_enabled
        assert vals[2] == pytest.approx(-40.0)         # gate_threshold_db
        assert vals[7] == 1                            # comp_enabled
        assert vals[9] == pytest.approx(4.0)           # comp_ratio
        assert vals[13] == 1                           # eq_enabled
        # eq bands start at index 14: (type,freq,gain,q) * 3
        assert vals[14] == 1                           # band0 lowshelf code
        assert vals[14 + 4] == 0                       # band1 peak code
        assert vals[14 + 4 + 2] == pytest.approx(6.0)  # band1 gain_db
        # tail: soundboard_enabled, mic_mute
        assert vals[-2] == 1
        assert vals[-1] == 0


def test_publish_params_reflects_mutes(bridge_path):
    eng = AudioEngine(sample_rate=SR, block_size=256, channels=2)
    eng.mic_channel.mute = True
    eng.soundboard_channel.mute = True
    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        br.publish_params(eng)
        vals = _unpack_params(br)
        assert vals[-2] == 0  # soundboard disabled
        assert vals[-1] == 1  # mic muted


# --- engine APO mode --------------------------------------------------------

def test_render_soundboard_excludes_mic(bridge_path):
    eng = AudioEngine(sample_rate=SR, block_size=256, channels=2)
    # A mic block passed to render_block would appear; render_soundboard must
    # ignore the mic entirely (the APO owns the mic).
    out = eng.render_soundboard(256)
    assert out.shape == (256, 2)
    assert np.allclose(out, 0.0)  # no voices, no mic => silence


def test_start_apo_creates_bridge_and_streams(bridge_path):
    eng = AudioEngine(sample_rate=SR, block_size=256, channels=2)
    eng.start_apo()
    try:
        assert eng.apo_mode is True
        assert eng.apo_bridge is not None
        assert os.path.exists(bridge_path)
    finally:
        eng.stop_apo()
    assert eng.apo_mode is False
    assert eng.apo_bridge is None


# --- DSP parity vs the C++ oracle -------------------------------------------

def _oracle(tag, sr, channels, params, block):
    tool = _tool("dsp_oracle")
    if tool is None:
        pytest.skip("apo/build/dsp_oracle not built")
    payload = tag.encode() + struct.pack("<III", sr, channels, len(params))
    payload += struct.pack("<%df" % len(params), *params)
    frames = block.shape[0]
    payload += struct.pack("<I", frames)
    payload += np.ascontiguousarray(block, dtype=np.float32).tobytes()
    res = subprocess.run([tool], input=payload, stdout=subprocess.PIPE, check=True)
    out = np.frombuffer(res.stdout, dtype=np.float32)
    return out.reshape(frames, channels)


def _rand_block(frames=1024, channels=2, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((frames, channels)) * 0.3).astype(np.float32)


def test_gate_parity():
    from discrod.audio.dsp import Gate
    block = _rand_block(seed=1)
    params = (-30.0, -60.0, 1.0, 20.0, 100.0)  # thr,range,atk,hold,rel
    ref = block.copy()
    g = Gate(SR, threshold_db=params[0], range_db=params[1], attack_ms=params[2],
             hold_ms=params[3], release_ms=params[4], enabled=True)
    g.process(ref)
    got = _oracle("GATE", SR, 2, params, block)
    assert np.max(np.abs(got - ref)) < 1e-4


def test_compressor_parity():
    from discrod.audio.dsp import Compressor
    block = _rand_block(seed=2)
    params = (-18.0, 4.0, 10.0, 120.0, 0.0)  # thr,ratio,atk,rel,makeup
    ref = block.copy()
    c = Compressor(SR, threshold_db=params[0], ratio=params[1],
                   attack_ms=params[2], release_ms=params[3],
                   makeup_db=params[4], enabled=True)
    c.process(ref)
    got = _oracle("COMP", SR, 2, params, block)
    assert np.max(np.abs(got - ref)) < 1e-4


@pytest.mark.parametrize("code,ftype,freq,gain,q", [
    (0, PEAK, 1000.0, 6.0, 1.0),
    (1, LOW_SHELF, 120.0, -4.0, 0.707),
    (2, HIGH_SHELF, 8000.0, 5.0, 0.707),
    (3, LOW_PASS, 4000.0, 0.0, 0.707),
])
def test_biquad_parity(code, ftype, freq, gain, q):
    from discrod.audio.dsp.biquad import Biquad
    block = _rand_block(seed=3)
    ref = block.copy()
    bq = Biquad(SR, ftype, freq, gain, q, channels=2)
    bq.process(ref)
    got = _oracle("BIQD", SR, 2, (float(code), freq, gain, q), block)
    assert np.max(np.abs(got - ref)) < 1e-4


# --- end-to-end writer/reader through a real mapping ------------------------

def test_bridge_probe_reads_written_soundboard(bridge_path):
    probe = _tool("bridge_probe")
    if probe is None:
        pytest.skip("apo/build/bridge_probe not built")

    eng = AudioEngine(sample_rate=SR, block_size=256, channels=2)
    with B.ApoBridge(sample_rate=SR, ring_channels=2) as br:
        br.publish_params(eng)  # passthrough mic, soundboard enabled
        # Queue enough soundboard audio to clear priming plus several blocks.
        value = 0.2
        total = B.RING_PRIME_FRAMES + 256 * 6
        br.write_soundboard(np.full((total, 2), value, dtype=np.float32))
        br._mm.flush()

        # Mic input is silence, so probe output should be the soundboard value
        # once priming is satisfied.
        blocks = 8
        res = subprocess.run(
            [probe, bridge_path, str(SR), "2", "256", str(blocks)],
            input=b"", stdout=subprocess.PIPE, check=True)
        out = np.frombuffer(res.stdout, dtype=np.float32).reshape(blocks * 256, 2)

    # The prime window (first ~4 blocks) is silent; a later block must carry the
    # soundboard signal at its written level.
    tail = out[B.RING_PRIME_FRAMES:B.RING_PRIME_FRAMES + 256]
    assert np.max(np.abs(tail)) == pytest.approx(0.2, abs=1e-3)
