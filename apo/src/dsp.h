// C++ ports of the app's mic DSP chain (app/discrod/audio/dsp/*.py).
//
// These run inside the APO's real-time processing call, so the rules match the
// Python implementation's hot path: no allocation, no locks, no I/O — just
// arithmetic on the interleaved float block.  State is kept in double
// precision exactly like the Python versions (plain Python floats / float64
// scipy state), so a parity harness can compare outputs sample-for-sample.
//
// Blocks are interleaved float32, `frames` frames wide by `channels` channels
// (the layout an APO receives), which is the same memory order as the app's
// (frames, channels) numpy arrays.

#pragma once

#include <cstdint>

#include "bridge_protocol.h"

namespace discrod {

constexpr uint32_t DSP_MAX_CHANNELS = 8;

double db_to_linear(double db);
// One-pole smoothing coefficient; matches dsp/dynamics.py _coeff().
double ballistics_coeff(double time_ms, uint32_t sample_rate);

// Downward expander / noise gate (dsp/dynamics.py Gate).
class Gate {
public:
    void configure(uint32_t sample_rate, float threshold_db, float range_db,
                   float attack_ms, float hold_ms, float release_ms);
    void reset();
    void process(float* block, uint32_t frames, uint32_t channels);

private:
    uint32_t sample_rate_ = 48000;
    double threshold_db_ = -45.0, range_db_ = -60.0;
    double attack_ms_ = 1.0, hold_ms_ = 50.0, release_ms_ = 120.0;
    double env_ = 0.0;       // smoothed gain (1 open, 0 closed)
    int64_t hold_count_ = 0;
};

// Feed-forward peak compressor (dsp/dynamics.py Compressor), including the
// DETECTOR_FLOOR_DB clamp and the floor-only env clamp at block entry.
class Compressor {
public:
    static constexpr double DETECTOR_FLOOR_DB = 20.0;

    void configure(uint32_t sample_rate, float threshold_db, float ratio,
                   float attack_ms, float release_ms, float makeup_db);
    void reset();
    void process(float* block, uint32_t frames, uint32_t channels);

private:
    uint32_t sample_rate_ = 48000;
    double threshold_db_ = -18.0, ratio_ = 3.0;
    double attack_ms_ = 10.0, release_ms_ = 120.0, makeup_db_ = 0.0;
    double env_db_ = -18.0 - DETECTOR_FLOOR_DB;
};

// One RBJ biquad section, transposed direct form II, per-channel state
// (dsp/biquad.py — including the Nyquist clamp and NaN-state recovery).
class Biquad {
public:
    void configure(uint32_t sample_rate, uint32_t type, float freq,
                   float gain_db, float q);
    void reset();
    void process(float* block, uint32_t frames, uint32_t channels);

private:
    double b0_ = 1.0, b1_ = 0.0, b2_ = 0.0, a1_ = 0.0, a2_ = 0.0;
    double z1_[DSP_MAX_CHANNELS] = {}, z2_[DSP_MAX_CHANNELS] = {};
};

// Cascaded 3-band EQ (dsp/eq.py).  Bands always process while the EQ is
// enabled — a flat band is an exact identity filter and skipping it would
// freeze stale state that pops when the gain leaves 0 dB.
class Equalizer {
public:
    void configure(uint32_t sample_rate, const BridgeEqBand (&bands)[EQ_BANDS]);
    void reset();
    void process(float* block, uint32_t frames, uint32_t channels);

private:
    Biquad bands_[EQ_BANDS];
};

}  // namespace discrod
