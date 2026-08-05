#include "apo_core.h"

#include <atomic>
#include <cmath>
#include <cstring>

namespace discrod {

// Aligned u32/f32 accessors over the shared mapping.  atomic_ref gives the
// acquire/release ordering the SPSC ring and seqlock need; on x86-64 these
// compile to plain MOVs plus compiler fences.
static inline uint32_t load_u32(const uint32_t& v) {
    return std::atomic_ref<const uint32_t>(v).load(std::memory_order_acquire);
}
static inline void store_u32(uint32_t& v, uint32_t x) {
    std::atomic_ref<uint32_t>(v).store(x, std::memory_order_release);
}
static inline void store_f32(float& v, float x) {
    std::atomic_ref<float>(v).store(x, std::memory_order_release);
}

bool ApoCore::attach(void* base, uint32_t mapped_size, uint32_t sample_rate,
                     uint32_t channels) {
    detach();
    sample_rate_ = sample_rate;
    channels_ = channels;
    gate_.reset();
    comp_.reset();
    eq_.reset();
    if (base == nullptr || mapped_size < BRIDGE_FILE_SIZE) return false;

    auto* header = static_cast<BridgeHeader*>(base);
    if (load_u32(header->magic) != BRIDGE_MAGIC ||
        load_u32(header->version) != BRIDGE_VERSION)
        return false;

    header_ = header;
    ring_ = reinterpret_cast<float*>(static_cast<char*>(base) + RING_OFFSET);

    // Publish our stream format so the app can render at the right rate.
    store_u32(header_->apo_sample_rate, sample_rate_);
    store_u32(header_->apo_channels, channels_);

    // Consume from wherever read_pos currently sits (the app zeroes it on
    // open; a re-attaching APO resumes where the previous one stopped).  A
    // large backlog accrued while no APO was consuming is bounded by the
    // ring-lap protection in mix_ring, so there is no need to hard-skip here.
    last_epoch_ = load_u32(header_->ring_epoch);
    primed_ = false;
    applied_seq_ = 0;
    have_params_ = false;
    refresh_params();
    return true;
}

void ApoCore::detach() {
    if (header_ != nullptr) {
        // Tell the app the APO is gone (heartbeat stops; rate reads 0).
        store_u32(header_->apo_sample_rate, 0);
        store_u32(header_->apo_channels, 0);
    }
    header_ = nullptr;
    ring_ = nullptr;
    have_params_ = false;
}

void ApoCore::refresh_params() {
    if (header_ == nullptr) return;
    const uint32_t seq0 = load_u32(header_->param_seq);
    if (seq0 == applied_seq_ && have_params_) return;
    if (seq0 & 1u) return;  // writer mid-update; keep current params

    // Seqlock read with one bounded retry — this runs on the RT thread, so on
    // contention we just keep last block's params and try again next block.
    BridgeParams fresh;
    std::memcpy(&fresh, reinterpret_cast<const char*>(header_) + PARAMS_OFFSET,
                sizeof(fresh));
    std::atomic_thread_fence(std::memory_order_acquire);
    const uint32_t seq1 = load_u32(header_->param_seq);
    if (seq1 != seq0) return;

    params_ = fresh;
    applied_seq_ = seq0;
    have_params_ = true;

    gate_.configure(sample_rate_, params_.gate_threshold_db,
                    params_.gate_range_db, params_.gate_attack_ms,
                    params_.gate_hold_ms, params_.gate_release_ms);
    comp_.configure(sample_rate_, params_.comp_threshold_db, params_.comp_ratio,
                    params_.comp_attack_ms, params_.comp_release_ms,
                    params_.comp_makeup_db);
    eq_.configure(sample_rate_, params_.eq_bands);
}

void ApoCore::process(float* block, uint32_t frames, uint32_t channels) {
    if (frames == 0 || channels == 0) return;
    refresh_params();

    // Mic chain, same order as the app's mic channel: gate -> comp -> EQ ->
    // gain.  Without a bridge (or before the app first publishes params) the
    // APO is a bit-exact passthrough.
    if (have_params_) {
        if (params_.mic_mute) {
            std::memset(block, 0, sizeof(float) * frames * channels);
        } else {
            if (params_.gate_enabled) gate_.process(block, frames, channels);
            if (params_.comp_enabled) comp_.process(block, frames, channels);
            if (params_.eq_enabled) eq_.process(block, frames, channels);
            const float g = static_cast<float>(db_to_linear(params_.mic_gain_db));
            if (g != 1.0f)
                for (uint32_t i = 0; i < frames * channels; ++i) block[i] *= g;
        }
    }

    if (header_ != nullptr) {
        mix_ring(block, frames, channels);

        // Hard safety clip: the sum of a hot mic and soundboard audio must
        // not wrap when a downstream stage converts to fixed point.
        float peak = 0.0f;
        for (uint32_t i = 0; i < frames * channels; ++i) {
            float s = block[i];
            if (s > 1.0f) s = 1.0f;
            else if (s < -1.0f) s = -1.0f;
            block[i] = s;
            const float a = s < 0.0f ? -s : s;
            if (a > peak) peak = a;
        }
        store_f32(header_->apo_meter, peak);
        store_u32(header_->apo_heartbeat, load_u32(header_->apo_heartbeat) + 1);
    }
}

void ApoCore::mix_ring(float* block, uint32_t frames, uint32_t channels) {
    if (!have_params_ || !params_.soundboard_enabled) return;

    const uint32_t epoch = load_u32(header_->ring_epoch);
    if (epoch != last_epoch_) {
        // App reformatted the ring: drop to live and re-prime.
        last_epoch_ = epoch;
        store_u32(header_->read_pos, load_u32(header_->write_pos));
        primed_ = false;
        return;
    }

    // No resampler in the RT path: consume only when the app renders at our
    // rate.  (attach() published the rate; the app re-renders to match.)
    if (load_u32(header_->ring_sample_rate) != sample_rate_) return;
    const uint32_t ring_channels = load_u32(header_->ring_channels);
    const uint32_t capacity = load_u32(header_->ring_capacity);
    if (ring_channels == 0 || ring_channels > RING_MAX_CHANNELS ||
        capacity == 0 || capacity > RING_CAPACITY_FRAMES)
        return;

    const uint32_t write = load_u32(header_->write_pos);
    uint32_t read = load_u32(header_->read_pos);
    uint32_t avail = write - read;  // u32 wrap-safe free-running counters
    if (avail > capacity) {
        // Writer lapped us (app restarted or we stalled): jump to live.
        read = write - capacity / 2;
        avail = capacity / 2;
        primed_ = false;
    }
    if (!primed_) {
        if (avail < RING_PRIME_FRAMES) return;  // keep building slack
        primed_ = true;
    }

    uint32_t todo = avail < frames ? avail : frames;
    for (uint32_t n = 0; n < todo; ++n) {
        const float* src = ring_ + ((read + n) % capacity) * ring_channels;
        float* dst = block + n * channels;
        if (ring_channels == channels) {
            for (uint32_t c = 0; c < channels; ++c) dst[c] += src[c];
        } else if (ring_channels == 1) {
            for (uint32_t c = 0; c < channels; ++c) dst[c] += src[0];
        } else {  // stereo ring into mono (or narrower) endpoint: average
            float sum = 0.0f;
            for (uint32_t c = 0; c < ring_channels; ++c) sum += src[c];
            const float mono = sum / static_cast<float>(ring_channels);
            for (uint32_t c = 0; c < channels; ++c) dst[c] += mono;
        }
    }
    store_u32(header_->read_pos, read + todo);
    if (todo < frames) primed_ = false;  // underrun: one clean gap, re-prime
}

}  // namespace discrod
