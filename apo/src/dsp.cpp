#include "dsp.h"

#include <cmath>

namespace discrod {

double db_to_linear(double db) { return std::pow(10.0, db / 20.0); }

double ballistics_coeff(double time_ms, uint32_t sample_rate) {
    if (time_ms < 0.01) time_ms = 0.01;
    return std::exp(-1.0 / (sample_rate * time_ms / 1000.0));
}

// Linked (max across channels) peak detector for one frame.
static inline float frame_peak(const float* frame, uint32_t channels) {
    float peak = 0.0f;
    for (uint32_t c = 0; c < channels; ++c) {
        float a = frame[c] < 0.0f ? -frame[c] : frame[c];
        if (a > peak) peak = a;
    }
    return peak;
}

// --- Gate -------------------------------------------------------------------

void Gate::configure(uint32_t sample_rate, float threshold_db, float range_db,
                     float attack_ms, float hold_ms, float release_ms) {
    sample_rate_ = sample_rate;
    threshold_db_ = threshold_db;
    range_db_ = range_db;
    attack_ms_ = attack_ms;
    hold_ms_ = hold_ms;
    release_ms_ = release_ms;
}

void Gate::reset() {
    env_ = 0.0;
    hold_count_ = 0;
}

void Gate::process(float* block, uint32_t frames, uint32_t channels) {
    const double thr = db_to_linear(threshold_db_);
    const double floor_lin = db_to_linear(range_db_);
    const double atk = ballistics_coeff(attack_ms_, sample_rate_);
    const double rel = ballistics_coeff(release_ms_, sample_rate_);
    const double one_m_atk = 1.0 - atk;
    const double one_m_rel = 1.0 - rel;
    const int64_t hold_samples =
        static_cast<int64_t>(sample_rate_ * hold_ms_ / 1000.0);

    double env = env_;
    int64_t hold = hold_count_;
    for (uint32_t n = 0; n < frames; ++n) {
        float* frame = block + n * channels;
        const double level = frame_peak(frame, channels);
        const double target = level >= thr ? 1.0 : floor_lin;
        if (target >= env) {
            hold = hold_samples;
            env = atk * env + one_m_atk * target;
        } else if (hold > 0) {
            --hold;
        } else {
            env = rel * env + one_m_rel * target;
        }
        const float g = static_cast<float>(env);
        for (uint32_t c = 0; c < channels; ++c) frame[c] *= g;
    }
    env_ = env;
    hold_count_ = hold;
}

// --- Compressor -------------------------------------------------------------

void Compressor::configure(uint32_t sample_rate, float threshold_db,
                           float ratio, float attack_ms, float release_ms,
                           float makeup_db) {
    sample_rate_ = sample_rate;
    threshold_db_ = threshold_db;
    ratio_ = ratio;
    attack_ms_ = attack_ms;
    release_ms_ = release_ms;
    makeup_db_ = makeup_db;
}

void Compressor::reset() { env_db_ = threshold_db_ - DETECTOR_FLOOR_DB; }

void Compressor::process(float* block, uint32_t frames, uint32_t channels) {
    const double atk = ballistics_coeff(attack_ms_, sample_rate_);
    const double rel = ballistics_coeff(release_ms_, sample_rate_);
    const double one_m_atk = 1.0 - atk;
    const double one_m_rel = 1.0 - rel;
    const double makeup = db_to_linear(makeup_db_);
    const double thr = threshold_db_;
    const double floor_db = thr - DETECTOR_FLOOR_DB;
    const double ratio = ratio_ < 1.0 ? 1.0 : ratio_;
    const double slope = 1.0 - 1.0 / ratio;

    // Clamp from below only: the envelope must keep tracking levels above
    // 0 dBFS or it re-attacks at every block boundary (see dynamics.py).
    double env_db = env_db_ < floor_db ? floor_db : env_db_;
    for (uint32_t n = 0; n < frames; ++n) {
        float* frame = block + n * channels;
        // Match the Python pipeline's float32 intermediate: detector and dB
        // conversion in single precision, envelope recursion in double.
        const float det = frame_peak(frame, channels);
        float level_f = 20.0f * std::log10(det + 1e-9f);
        double level_db = level_f;
        if (level_db < floor_db) level_db = floor_db;
        if (level_db > env_db)
            env_db = atk * env_db + one_m_atk * level_db;
        else
            env_db = rel * env_db + one_m_rel * level_db;
        const double over = env_db - thr;
        const double gain_db = over > 0.0 ? -over * slope : 0.0;
        const float g =
            static_cast<float>(std::pow(10.0, gain_db / 20.0) * makeup);
        for (uint32_t c = 0; c < channels; ++c) frame[c] *= g;
    }
    env_db_ = env_db;
}

// --- Biquad -----------------------------------------------------------------

void Biquad::configure(uint32_t sample_rate, uint32_t type, float freq,
                       float gain_db, float q) {
    // RBJ cookbook, mirroring biquad.py _update() including the Nyquist clamp.
    constexpr double MAX_FREQ_RATIO = 0.45;
    const double a = std::pow(10.0, gain_db / 40.0);
    double f = freq;
    if (f < 1.0) f = 1.0;
    const double fmax = MAX_FREQ_RATIO * sample_rate;
    if (f > fmax) f = fmax;
    const double w0 = 2.0 * M_PI * f / sample_rate;
    const double cw = std::cos(w0);
    const double sw = std::sin(w0);
    const double qq = q < 1e-4 ? 1e-4 : q;
    const double alpha = sw / (2.0 * qq);

    double b0, b1, b2, a0, a1, a2;
    switch (type) {
        case BAND_PEAK:
            b0 = 1 + alpha * a;
            b1 = -2 * cw;
            b2 = 1 - alpha * a;
            a0 = 1 + alpha / a;
            a1 = -2 * cw;
            a2 = 1 - alpha / a;
            break;
        case BAND_LOW_SHELF: {
            const double ap1 = a + 1, am1 = a - 1;
            const double ta = 2 * std::sqrt(a) * alpha;
            b0 = a * (ap1 - am1 * cw + ta);
            b1 = 2 * a * (am1 - ap1 * cw);
            b2 = a * (ap1 - am1 * cw - ta);
            a0 = ap1 + am1 * cw + ta;
            a1 = -2 * (am1 + ap1 * cw);
            a2 = ap1 + am1 * cw - ta;
            break;
        }
        case BAND_HIGH_SHELF: {
            const double ap1 = a + 1, am1 = a - 1;
            const double ta = 2 * std::sqrt(a) * alpha;
            b0 = a * (ap1 + am1 * cw + ta);
            b1 = -2 * a * (am1 + ap1 * cw);
            b2 = a * (ap1 + am1 * cw - ta);
            a0 = ap1 - am1 * cw + ta;
            a1 = 2 * (am1 - ap1 * cw);
            a2 = ap1 - am1 * cw - ta;
            break;
        }
        case BAND_LOW_PASS:
            b0 = (1 - cw) / 2;
            b1 = 1 - cw;
            b2 = (1 - cw) / 2;
            a0 = 1 + alpha;
            a1 = -2 * cw;
            a2 = 1 - alpha;
            break;
        case BAND_HIGH_PASS:
            b0 = (1 + cw) / 2;
            b1 = -(1 + cw);
            b2 = (1 + cw) / 2;
            a0 = 1 + alpha;
            a1 = -2 * cw;
            a2 = 1 - alpha;
            break;
        default:  // unknown type from a torn/hostile param block: identity
            b0 = 1.0; b1 = 0.0; b2 = 0.0; a0 = 1.0; a1 = 0.0; a2 = 0.0;
            break;
    }
    b0_ = b0 / a0;
    b1_ = b1 / a0;
    b2_ = b2 / a0;
    a1_ = a1 / a0;
    a2_ = a2 / a0;
}

void Biquad::reset() {
    for (uint32_t c = 0; c < DSP_MAX_CHANNELS; ++c) z1_[c] = z2_[c] = 0.0;
}

void Biquad::process(float* block, uint32_t frames, uint32_t channels) {
    if (channels > DSP_MAX_CHANNELS) channels = DSP_MAX_CHANNELS;
    // NaN-state recovery, as in biquad.py: a previously unstable
    // configuration must not poison the output forever.
    for (uint32_t c = 0; c < channels; ++c) {
        if (!std::isfinite(z1_[c]) || !std::isfinite(z2_[c])) {
            reset();
            break;
        }
    }
    for (uint32_t c = 0; c < channels; ++c) {
        double z1 = z1_[c], z2 = z2_[c];
        float* p = block + c;
        for (uint32_t n = 0; n < frames; ++n, p += channels) {
            const double x = *p;
            const double y = b0_ * x + z1;
            z1 = b1_ * x - a1_ * y + z2;
            z2 = b2_ * x - a2_ * y;
            *p = static_cast<float>(y);
        }
        z1_[c] = z1;
        z2_[c] = z2;
    }
}

// --- Equalizer --------------------------------------------------------------

void Equalizer::configure(uint32_t sample_rate,
                          const BridgeEqBand (&bands)[EQ_BANDS]) {
    for (uint32_t i = 0; i < EQ_BANDS; ++i)
        bands_[i].configure(sample_rate, bands[i].type, bands[i].freq,
                            bands[i].gain_db, bands[i].q);
}

void Equalizer::reset() {
    for (auto& b : bands_) b.reset();
}

void Equalizer::process(float* block, uint32_t frames, uint32_t channels) {
    for (auto& b : bands_) b.process(block, frames, channels);
}

}  // namespace discrod
