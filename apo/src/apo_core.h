// Platform-independent core of the Discrod capture APO.
//
// ApoCore is everything the APO does per block except the COM plumbing: read
// the param seqlock, run the mic DSP chain, consume soundboard audio from the
// shared-memory ring, mix, clip, and publish heartbeat/meter.  Keeping it free
// of Windows headers means the exact code that runs inside audiodg.exe can be
// driven by the Linux test harness against the Python bridge writer.
//
// Threading: everything here runs on the audio engine's RT thread.  The only
// cross-process synchronization is via the mapped BridgeHeader fields (see
// bridge_protocol.h for the single-writer-per-field rules).

#pragma once

#include <cstdint>

#include "bridge_protocol.h"
#include "dsp.h"

namespace discrod {

class ApoCore {
public:
    // `base` is the start of the bridge mapping (or nullptr for "no bridge":
    // process() then applies DSP defaults only — i.e. passthrough).
    // Returns false if the mapping fails validation (wrong magic/version),
    // in which case the core behaves as if there were no bridge.
    bool attach(void* base, uint32_t mapped_size, uint32_t sample_rate,
                uint32_t channels);
    void detach();

    // Process one interleaved block in place: mic DSP + soundboard mix + clip.
    void process(float* block, uint32_t frames, uint32_t channels);

    bool attached() const { return header_ != nullptr; }

private:
    void refresh_params();
    // Mixes up to `frames` ring frames into `block`; silence on underrun,
    // rate mismatch, or when the soundboard is disabled.
    void mix_ring(float* block, uint32_t frames, uint32_t channels);

    BridgeHeader* header_ = nullptr;
    float* ring_ = nullptr;
    uint32_t sample_rate_ = 48000;
    uint32_t channels_ = 2;

    BridgeParams params_{};
    uint32_t applied_seq_ = 0;
    bool have_params_ = false;

    uint32_t last_epoch_ = 0;
    bool primed_ = false;

    Gate gate_;
    Compressor comp_;
    Equalizer eq_;
};

}  // namespace discrod
