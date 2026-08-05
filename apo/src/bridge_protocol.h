// Shared-memory bridge protocol between the Discrod app (writer) and the
// Discrod capture APO (reader).
//
// Transport: a plain file-backed memory mapping at a fixed path
// (%ProgramData%\Discrod\apo_bridge.bin).  A file mapping is used instead of a
// named section because the APO runs inside audiodg.exe (LOCAL SERVICE,
// session 0) while the app runs in the user session: cross-session named
// objects need the Global\ namespace plus a hand-built DACL, whereas a file
// under ProgramData is creatable by any interactive user and openable by
// services with no privilege juggling.  The mapping is small and rewritten
// continuously, so file-cache writeback traffic is negligible.
//
// Layout (little-endian, fixed offsets — never reorder, only append):
//
//   [0,   64)   BridgeHeader
//   [64,  256)  reserved
//   [256, 4096) BridgeParams (+ reserved tail)
//   [4096, ...) audio ring: RING_CAPACITY_FRAMES * RING_MAX_CHANNELS floats,
//               frame-interleaved at ring_channels wide (unused tail when
//               ring_channels < RING_MAX_CHANNELS)
//
// Concurrency model — strictly one writer per field:
//   * The app owns: ring format fields, write_pos, param block + param_seq.
//   * The APO owns: apo_* fields, read_pos, meter.
//   All cross-process fields are 4-byte aligned u32/f32 (or an 8-byte aligned
//   u64 the APO alone writes), so aligned x86-64 stores from either side are
//   atomic; the ring itself is SPSC with release/acquire ordering provided by
//   the position variables.
//   * Ring positions are free-running u32 FRAME counters (wrap at 2^32,
//     capacity << 2^32 so (write - read) is always the true fill).
//   * The param block is guarded by a seqlock: the app bumps param_seq to an
//     odd value, writes the block, then bumps it even.  The APO copies the
//     block and retries (bounded) if the sequence moved or is odd.
//
// Startup/ownership: the app creates and zeroes the file, stamps magic last.
// The APO validates magic+version at LockForProcess and publishes its stream
// format (apo_sample_rate/apo_channels); the app is expected to re-render at
// that rate.  The APO consumes ring audio only while ring_sample_rate matches
// its own rate (channel-count mismatches are up/down-mixed, rate mismatches
// are silence — there is no resampler in the RT path).

#pragma once

#include <cstdint>

namespace discrod {

constexpr uint32_t BRIDGE_MAGIC = 0x4F504144u;  // 'DAPO' little-endian
constexpr uint32_t BRIDGE_VERSION = 1;

constexpr uint32_t HEADER_OFFSET = 0;
constexpr uint32_t PARAMS_OFFSET = 256;
constexpr uint32_t RING_OFFSET = 4096;

// Ring sizing: 16384 frames = ~341 ms at 48 kHz — enough slack for a sloppy
// app-side scheduler, small enough that the drop-to-target logic keeps
// worst-case soundboard latency well under the file size budget.
constexpr uint32_t RING_CAPACITY_FRAMES = 16384;
constexpr uint32_t RING_MAX_CHANNELS = 2;

constexpr uint32_t BRIDGE_FILE_SIZE =
    RING_OFFSET + RING_CAPACITY_FRAMES * RING_MAX_CHANNELS * sizeof(float);

// Frames the APO accumulates before it starts consuming the ring (and the
// re-priming threshold after an underrun): absorbs app scheduler jitter the
// same way the engine's mic ring priming does.
constexpr uint32_t RING_PRIME_FRAMES = 1024;

// EQ band types (must match app/discrod/audio/dsp/biquad.py).
enum BandType : uint32_t {
    BAND_PEAK = 0,
    BAND_LOW_SHELF = 1,
    BAND_HIGH_SHELF = 2,
    BAND_LOW_PASS = 3,
    BAND_HIGH_PASS = 4,
};

constexpr uint32_t EQ_BANDS = 3;

#pragma pack(push, 4)

struct BridgeHeader {
    uint32_t magic;             // 0:  BRIDGE_MAGIC once the file is initialized
    uint32_t version;           // 4:  BRIDGE_VERSION
    // --- APO -> app ---------------------------------------------------------
    uint32_t apo_sample_rate;   // 8:  endpoint stream rate (0 = APO not loaded)
    uint32_t apo_channels;      // 12: endpoint channel count
    uint32_t apo_heartbeat;     // 16: ++ every APOProcess call
    float    apo_meter;         // 20: post-mix peak (linear), UI feedback
    // --- app -> APO: ring format --------------------------------------------
    uint32_t ring_sample_rate;  // 24: rate the ring audio was rendered at
    uint32_t ring_channels;     // 28: 1..RING_MAX_CHANNELS
    uint32_t ring_capacity;     // 32: frames (== RING_CAPACITY_FRAMES)
    uint32_t ring_epoch;        // 36: app bumps after reformatting the ring
    // --- ring positions (free-running frame counters) -----------------------
    uint32_t write_pos;         // 40: app-owned
    uint32_t read_pos;          // 44: APO-owned
    // --- params seqlock ------------------------------------------------------
    uint32_t param_seq;         // 48: odd while the app is writing
    uint32_t reserved0;         // 52
    uint32_t reserved1;         // 56
    uint32_t reserved2;         // 60
};

struct BridgeEqBand {
    uint32_t type;              // BandType
    float    freq;
    float    gain_db;
    float    q;
};

// Mirrors the app's mic-channel chain (gate -> compressor -> EQ -> gain) plus
// a soundboard-mix switch.  The app publishes; the APO applies to the raw mic
// signal.  Master gain is NOT mirrored: the ring audio arrives already
// mastered by the app, and the mic level is governed by mic_gain_db.
struct BridgeParams {
    float    mic_gain_db;

    uint32_t gate_enabled;
    float    gate_threshold_db;
    float    gate_range_db;
    float    gate_attack_ms;
    float    gate_hold_ms;
    float    gate_release_ms;

    uint32_t comp_enabled;
    float    comp_threshold_db;
    float    comp_ratio;
    float    comp_attack_ms;
    float    comp_release_ms;
    float    comp_makeup_db;

    uint32_t eq_enabled;
    BridgeEqBand eq_bands[EQ_BANDS];

    uint32_t soundboard_enabled;  // 0 = don't mix ring audio (mic passthrough)
    uint32_t mic_mute;            // 1 = replace mic signal with silence
};

#pragma pack(pop)

static_assert(sizeof(BridgeHeader) == 64, "header layout drifted");
static_assert(sizeof(BridgeEqBand) == 16, "band layout drifted");
static_assert(sizeof(BridgeParams) == 4 * 16 + sizeof(BridgeEqBand) * EQ_BANDS,
              "params layout drifted");
static_assert(PARAMS_OFFSET + sizeof(BridgeParams) < RING_OFFSET,
              "params overflow into ring");

}  // namespace discrod
