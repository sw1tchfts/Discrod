// Correctness tests for the platform-independent APO core.
//
// These do not need Windows: they exercise the DSP behaviour and the
// shared-memory ring/seqlock protocol that ApoCore implements, driving the
// bridge through an in-process buffer the way the app's writer would.  Sample-
// for-sample parity against the Python DSP is checked separately by
// app/tests/test_apo_bridge.py, which pipes vectors through the dsp_oracle
// tool built from this same core.

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

#include "apo_core.h"
#include "bridge_protocol.h"
#include "dsp.h"

using namespace discrod;

static int g_failures = 0;

static void check(bool cond, const char* what) {
    if (!cond) {
        std::printf("FAIL: %s\n", what);
        ++g_failures;
    }
}

static float block_peak(const std::vector<float>& b) {
    float p = 0.0f;
    for (float s : b) p = std::max(p, std::fabs(s));
    return p;
}

// --- DSP behaviour ----------------------------------------------------------

static void test_gate_closes_and_opens() {
    Gate gate;
    gate.configure(48000, -45.0f, -60.0f, 1.0f, 0.0f, 50.0f);
    gate.reset();

    // Quiet signal (below threshold) should be strongly attenuated.
    std::vector<float> quiet(2048, 0.0005f);  // ~ -66 dBFS
    gate.process(quiet.data(), 1024, 2);
    check(block_peak(quiet) < 0.0005f * 0.5f, "gate attenuates sub-threshold");

    // Loud signal should pass near unity after the attack.
    Gate gate2;
    gate2.configure(48000, -45.0f, -60.0f, 1.0f, 0.0f, 50.0f);
    gate2.reset();
    std::vector<float> loud(4096, 0.5f);
    gate2.process(loud.data(), 2048, 2);
    check(std::fabs(loud[4094] - 0.5f) < 0.01f, "gate opens for loud signal");
}

static void test_compressor_reduces_gain() {
    Compressor comp;
    comp.configure(48000, -18.0f, 4.0f, 5.0f, 80.0f, 0.0f);
    comp.reset();
    // Hot signal well above threshold: steady-state output must be attenuated.
    std::vector<float> hot(9600 * 2, 0.7f);  // ~ -3 dBFS, above -18 dB thr
    comp.process(hot.data(), 9600, 2);
    check(std::fabs(hot.back()) < 0.7f * 0.9f, "compressor pulls down hot signal");
}

static void test_compressor_reengages_after_pause() {
    // The DETECTOR_FLOOR_DB clamp must keep the envelope near threshold across
    // a silent gap so the next onset is compressed within a few ms.
    Compressor comp;
    comp.configure(48000, -18.0f, 4.0f, 5.0f, 120.0f, 0.0f);
    comp.reset();
    std::vector<float> loud(2 * 4800, 0.7f);
    comp.process(loud.data(), 4800, 2);          // establish gain reduction
    std::vector<float> silence(2 * 4800, 0.0f);
    comp.process(silence.data(), 4800, 2);       // 100 ms pause
    std::vector<float> onset(2 * 480, 0.7f);     // 10 ms onset
    comp.process(onset.data(), 480, 2);
    // Within 10 ms the compressor should already be attenuating the onset.
    check(std::fabs(onset.back()) < 0.7f * 0.95f,
          "compressor re-engages within ~10ms after a pause");
}

static void test_biquad_stable_and_flat_is_identity() {
    Biquad flat;
    flat.configure(48000, BAND_PEAK, 1000.0f, 0.0f, 1.0f);  // 0 dB = identity
    flat.reset();
    std::vector<float> in(512), copy;
    for (size_t i = 0; i < in.size(); ++i)
        in[i] = std::sin(0.1f * static_cast<float>(i));
    copy = in;
    flat.process(in.data(), 256, 2);
    float maxdiff = 0.0f;
    for (size_t i = 0; i < in.size(); ++i)
        maxdiff = std::max(maxdiff, std::fabs(in[i] - copy[i]));
    check(maxdiff < 1e-5f, "flat peak biquad is identity");

    // Absurd frequency must be clamped below Nyquist, not blow up.
    Biquad hi;
    hi.configure(8000, BAND_LOW_PASS, 100000.0f, 0.0f, 0.707f);
    hi.reset();
    std::vector<float> loud(2000, 1.0f);
    hi.process(loud.data(), 1000, 2);
    check(std::isfinite(block_peak(loud)) && block_peak(loud) < 10.0f,
          "biquad clamps super-Nyquist frequency (no runaway)");
}

// --- bridge ring / seqlock --------------------------------------------------

struct FakeBridge {
    std::vector<char> mem;
    BridgeHeader* hdr;
    float* ring;

    FakeBridge() : mem(BRIDGE_FILE_SIZE, 0) {
        hdr = reinterpret_cast<BridgeHeader*>(mem.data());
        ring = reinterpret_cast<float*>(mem.data() + RING_OFFSET);
        hdr->magic = BRIDGE_MAGIC;
        hdr->version = BRIDGE_VERSION;
        hdr->ring_sample_rate = 48000;
        hdr->ring_channels = 2;
        hdr->ring_capacity = RING_CAPACITY_FRAMES;
    }

    // App-side writer: append `frames` stereo frames of constant value.
    void write(uint32_t frames, float value) {
        uint32_t w = hdr->write_pos;
        for (uint32_t n = 0; n < frames; ++n) {
            float* dst = ring + ((w + n) % RING_CAPACITY_FRAMES) * 2;
            dst[0] = value;
            dst[1] = value;
        }
        hdr->write_pos = w + frames;
    }

    void set_params(const BridgeParams& p) {
        hdr->param_seq += 1;  // odd: writing
        std::memcpy(mem.data() + PARAMS_OFFSET, &p, sizeof(p));
        hdr->param_seq += 1;  // even: done
    }
};

static BridgeParams passthrough_params() {
    BridgeParams p{};
    p.mic_gain_db = 0.0f;
    p.gate_enabled = 0;
    p.comp_enabled = 0;
    p.eq_enabled = 0;
    p.soundboard_enabled = 1;
    p.mic_mute = 0;
    p.eq_bands[0] = {BAND_LOW_SHELF, 120.0f, 0.0f, 0.707f};
    p.eq_bands[1] = {BAND_PEAK, 1000.0f, 0.0f, 1.0f};
    p.eq_bands[2] = {BAND_HIGH_SHELF, 8000.0f, 0.0f, 0.707f};
    return p;
}

static void test_ring_mixes_after_priming() {
    FakeBridge bridge;
    bridge.set_params(passthrough_params());

    ApoCore core;
    check(core.attach(bridge.mem.data(), BRIDGE_FILE_SIZE, 48000, 2),
          "core attaches to a valid bridge");

    // Below the prime threshold: soundboard must not be consumed yet.
    bridge.write(RING_PRIME_FRAMES / 2, 0.25f);
    std::vector<float> block(256 * 2, 0.0f);
    core.process(block.data(), 256, 2);
    check(block_peak(block) == 0.0f, "no soundboard output before priming");

    // Cross the prime threshold, then a block should carry the mix.
    bridge.write(RING_PRIME_FRAMES, 0.25f);
    std::vector<float> block2(256 * 2, 0.0f);
    core.process(block2.data(), 256, 2);
    check(std::fabs(block_peak(block2) - 0.25f) < 1e-4f,
          "soundboard audio mixed after priming");

    // attach() set read_pos to the write_pos at that time (0); the first
    // block consumed nothing (pre-prime), the second consumed one block.
    check(bridge.hdr->read_pos == 256, "read_pos advances by consumed frames");
    check(bridge.hdr->apo_heartbeat >= 2, "heartbeat increments per process");
}

static void test_ring_underrun_is_clean_and_reprimes() {
    FakeBridge bridge;
    bridge.set_params(passthrough_params());
    ApoCore core;
    core.attach(bridge.mem.data(), BRIDGE_FILE_SIZE, 48000, 2);

    bridge.write(RING_PRIME_FRAMES, 0.5f);  // exactly the prime cushion
    std::vector<float> b(256 * 2, 0.0f);
    for (int i = 0; i < RING_PRIME_FRAMES / 256; ++i)
        core.process(b.data(), 256, 2);  // drain the ring to empty
    check(bridge.hdr->read_pos == RING_PRIME_FRAMES, "ring fully drained");

    // Next block underruns (avail == 0): must emit a clean gap and re-prime.
    std::vector<float> b2(256 * 2, 0.0f);
    core.process(b2.data(), 256, 2);
    check(block_peak(b2) == 0.0f, "underrun emits silence");

    // After an underrun the core re-primes: a fresh small write stays silent
    // until the prime threshold is crossed again.
    bridge.write(64, 0.5f);
    std::vector<float> b3(256 * 2, 0.0f);
    core.process(b3.data(), 256, 2);
    check(block_peak(b3) == 0.0f, "re-primes after underrun (one clean gap)");
}

static void test_soundboard_disabled_is_mic_passthrough() {
    FakeBridge bridge;
    BridgeParams p = passthrough_params();
    p.soundboard_enabled = 0;
    bridge.set_params(p);
    bridge.write(RING_PRIME_FRAMES * 2, 0.9f);  // plenty queued, but disabled

    ApoCore core;
    core.attach(bridge.mem.data(), BRIDGE_FILE_SIZE, 48000, 2);
    std::vector<float> mic(256 * 2, 0.1f);
    core.process(mic.data(), 256, 2);
    // Mic passes through unchanged; no soundboard added.
    check(std::fabs(block_peak(mic) - 0.1f) < 1e-4f,
          "soundboard disabled => mic passthrough only");
}

static void test_rate_mismatch_produces_no_soundboard() {
    FakeBridge bridge;
    bridge.set_params(passthrough_params());
    bridge.hdr->ring_sample_rate = 44100;  // app rendering at wrong rate
    bridge.write(RING_PRIME_FRAMES * 2, 0.8f);

    ApoCore core;
    core.attach(bridge.mem.data(), BRIDGE_FILE_SIZE, 48000, 2);
    std::vector<float> block(256 * 2, 0.0f);
    core.process(block.data(), 256, 2);
    check(block_peak(block) == 0.0f, "rate mismatch => no soundboard (no resampler)");
}

static void test_output_is_clipped() {
    FakeBridge bridge;
    bridge.set_params(passthrough_params());
    bridge.write(RING_PRIME_FRAMES * 2, 0.9f);
    ApoCore core;
    core.attach(bridge.mem.data(), BRIDGE_FILE_SIZE, 48000, 2);
    std::vector<float> mic(256 * 2, 0.9f);  // 0.9 mic + 0.9 board = 1.8
    core.process(mic.data(), 256, 2);
    check(block_peak(mic) <= 1.0f + 1e-6f, "sum is hard-clipped to [-1, 1]");
}

static void test_invalid_magic_is_passthrough() {
    FakeBridge bridge;
    bridge.hdr->magic = 0xDEADBEEF;
    ApoCore core;
    check(!core.attach(bridge.mem.data(), BRIDGE_FILE_SIZE, 48000, 2),
          "attach rejects bad magic");
    std::vector<float> mic(256 * 2, 0.2f);
    core.process(mic.data(), 256, 2);  // must not crash, must pass through
    check(std::fabs(block_peak(mic) - 0.2f) < 1e-6f,
          "no bridge => bit-exact passthrough");
}

int main() {
    test_gate_closes_and_opens();
    test_compressor_reduces_gain();
    test_compressor_reengages_after_pause();
    test_biquad_stable_and_flat_is_identity();
    test_ring_mixes_after_priming();
    test_ring_underrun_is_clean_and_reprimes();
    test_soundboard_disabled_is_mic_passthrough();
    test_rate_mismatch_produces_no_soundboard();
    test_output_is_clipped();
    test_invalid_magic_is_passthrough();

    if (g_failures == 0) {
        std::printf("apo_tests: all passed\n");
        return 0;
    }
    std::printf("apo_tests: %d failure(s)\n", g_failures);
    return 1;
}
