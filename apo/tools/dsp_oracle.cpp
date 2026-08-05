// dsp_oracle — apply one C++ DSP processor to a stream of samples, for
// cross-language parity testing against the Python DSP.
//
// Protocol (stdin/stdout, little-endian binary; keeps the harness free of any
// float-formatting drift):
//
//   stdin:
//     char[4]  processor tag: "GATE" | "COMP" | "BIQD"
//     u32      sample_rate
//     u32      channels
//     u32      num_params
//     f32[np]  params (see below)
//     u32      num_frames
//     f32[frames*channels]  interleaved input
//
//   params by tag:
//     GATE: threshold_db range_db attack_ms hold_ms release_ms
//     COMP: threshold_db ratio attack_ms release_ms makeup_db
//     BIQD: type freq gain_db q          (type per BandType)
//
//   stdout:
//     f32[frames*channels]  interleaved output
//
// The app-side test (app/tests/test_apo_bridge.py) drives this with random
// vectors and asserts the output matches the Python processors within a tight
// tolerance, proving the two DSP paths stay in lockstep.

#include <cstdint>
#include <cstdio>
#include <vector>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#endif

#include "dsp.h"

using namespace discrod;

template <typename T>
static bool read_exact(T* dst, size_t count) {
    return std::fread(dst, sizeof(T), count, stdin) == count;
}

int main() {
#if defined(_WIN32)
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    char tag[4];
    uint32_t sample_rate = 0, channels = 0, num_params = 0;
    if (!read_exact(tag, 4) || !read_exact(&sample_rate, 1) ||
        !read_exact(&channels, 1) || !read_exact(&num_params, 1))
        return 1;
    std::vector<float> params(num_params);
    if (num_params && !read_exact(params.data(), num_params)) return 1;
    uint32_t frames = 0;
    if (!read_exact(&frames, 1)) return 1;
    std::vector<float> buf(static_cast<size_t>(frames) * channels);
    if (!buf.empty() && !read_exact(buf.data(), buf.size())) return 1;

    auto p = [&](size_t i) { return i < params.size() ? params[i] : 0.0f; };

    if (tag[0] == 'G' && tag[1] == 'A') {
        Gate g;
        g.configure(sample_rate, p(0), p(1), p(2), p(3), p(4));
        g.reset();
        g.process(buf.data(), frames, channels);
    } else if (tag[0] == 'C' && tag[1] == 'O') {
        Compressor c;
        c.configure(sample_rate, p(0), p(1), p(2), p(3), p(4));
        c.reset();
        c.process(buf.data(), frames, channels);
    } else if (tag[0] == 'B' && tag[1] == 'I') {
        Biquad b;
        b.configure(sample_rate, static_cast<uint32_t>(p(0)), p(1), p(2), p(3));
        b.reset();
        b.process(buf.data(), frames, channels);
    } else {
        return 2;
    }

    if (!buf.empty())
        std::fwrite(buf.data(), sizeof(float), buf.size(), stdout);
    return 0;
}
