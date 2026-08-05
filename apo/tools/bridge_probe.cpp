// bridge_probe — attach ApoCore to a real bridge file and run N blocks,
// emitting the mixed output.  Used by app/tests/test_apo_bridge.py to prove
// the Python writer and the C++ APO agree through an actual memory mapping
// (the same mechanism as audiodg.exe mapping ProgramData\Discrod\apo_bridge.bin
// on Windows), on the platforms the test suite runs on.
//
// Usage: bridge_probe <path> <sample_rate> <channels> <frames_per_block> <blocks>
//   stdin:  optional mic input, blocks*frames*channels interleaved float32;
//           if EOF is hit early the remaining mic input is treated as silence.
//   stdout: blocks*frames*channels interleaved float32 (post mic-DSP + mix).

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#if defined(_WIN32)
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

#include "apo_core.h"
#include "bridge_protocol.h"

using namespace discrod;

int main(int argc, char** argv) {
    if (argc < 6) {
        std::fprintf(stderr,
            "usage: bridge_probe <path> <sr> <ch> <frames> <blocks>\n");
        return 2;
    }
    const char* path = argv[1];
    const uint32_t sr = std::strtoul(argv[2], nullptr, 10);
    const uint32_t ch = std::strtoul(argv[3], nullptr, 10);
    const uint32_t frames = std::strtoul(argv[4], nullptr, 10);
    const uint32_t blocks = std::strtoul(argv[5], nullptr, 10);

    void* base = nullptr;
#if defined(_WIN32)
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    HANDLE f = CreateFileA(path, GENERIC_READ | GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return 1;
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READWRITE, 0,
                                  BRIDGE_FILE_SIZE, nullptr);
    if (m == nullptr) return 1;
    base = MapViewOfFile(m, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0,
                         BRIDGE_FILE_SIZE);
#else
    int fd = open(path, O_RDWR);
    if (fd < 0) return 1;
    base = mmap(nullptr, BRIDGE_FILE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
                fd, 0);
    if (base == MAP_FAILED) return 1;
#endif

    ApoCore core;
    if (!core.attach(base, BRIDGE_FILE_SIZE, sr, ch)) {
        std::fprintf(stderr, "attach failed (bad magic/version?)\n");
        return 1;
    }

    std::vector<float> block(static_cast<size_t>(frames) * ch);
    for (uint32_t b = 0; b < blocks; ++b) {
        // Mic input from stdin (silence once stdin is exhausted).
        size_t got = std::fread(block.data(), sizeof(float), block.size(), stdin);
        for (size_t i = got; i < block.size(); ++i) block[i] = 0.0f;
        core.process(block.data(), frames, ch);
        std::fwrite(block.data(), sizeof(float), block.size(), stdout);
    }
    return 0;
}
