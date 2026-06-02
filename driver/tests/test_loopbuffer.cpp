// Portable host-side verification of the loopback ring algorithm.
//
// The kernel module (loopbuffer.cpp) can only be compiled with the WDK, but the
// ring's index arithmetic is plain logic.  This test mirrors that exact math
// (write/read/wrap/overrun/underrun) so we can prove it correct with a normal
// C++ compiler:  g++ -std=c++17 test_loopbuffer.cpp -o t && ./t
//
// Keep this in sync with CLoopbackBuffer in ../src/loopbuffer.cpp.

#include <cassert>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <vector>
#include <algorithm>

class RingRef {
public:
    explicit RingRef(uint32_t cap)
        : data_(cap, 0), capacity_(cap), writePos_(0), readPos_(0), count_(0) {}

    void Write(const uint8_t* src, uint32_t n) {
        if (n == 0) return;
        uint32_t toWrite = n, offset = 0;
        if (toWrite > capacity_) { offset = toWrite - capacity_; toWrite = capacity_; }
        uint32_t firstChunk = std::min(toWrite, capacity_ - writePos_);
        memcpy(&data_[writePos_], src + offset, firstChunk);
        if (toWrite > firstChunk)
            memcpy(&data_[0], src + offset + firstChunk, toWrite - firstChunk);
        writePos_ = (writePos_ + toWrite) % capacity_;
        count_ += toWrite;
        if (count_ > capacity_) {
            uint32_t drop = count_ - capacity_;
            readPos_ = (readPos_ + drop) % capacity_;
            count_ = capacity_;
        }
    }

    void Read(uint8_t* dst, uint32_t n) {
        uint32_t avail = std::min(n, count_);
        if (avail > 0) {
            uint32_t firstChunk = std::min(avail, capacity_ - readPos_);
            memcpy(dst, &data_[readPos_], firstChunk);
            if (avail > firstChunk)
                memcpy(dst + firstChunk, &data_[0], avail - firstChunk);
            readPos_ = (readPos_ + avail) % capacity_;
            count_ -= avail;
        }
        if (avail < n) memset(dst + avail, 0, n - avail);
    }

    uint32_t count() const { return count_; }

private:
    std::vector<uint8_t> data_;
    uint32_t capacity_, writePos_, readPos_, count_;
};

static void test_basic_roundtrip() {
    RingRef r(16);
    uint8_t in[8] = {1,2,3,4,5,6,7,8}, out[8] = {0};
    r.Write(in, 8);
    assert(r.count() == 8);
    r.Read(out, 8);
    assert(memcmp(in, out, 8) == 0);
    assert(r.count() == 0);
}

static void test_underrun_zero_fills() {
    RingRef r(16);
    uint8_t in[4] = {9,9,9,9}, out[8];
    memset(out, 0xAA, sizeof(out));
    r.Write(in, 4);
    r.Read(out, 8);                 // ask for more than available
    for (int i = 0; i < 4; ++i) assert(out[i] == 9);
    for (int i = 4; i < 8; ++i) assert(out[i] == 0);   // silence padding
}

static void test_wraparound() {
    RingRef r(8);
    uint8_t a[6] = {1,2,3,4,5,6}, scratch[6];
    r.Write(a, 6);
    r.Read(scratch, 6);            // advance read cursor past the midpoint
    uint8_t b[6] = {10,11,12,13,14,15}, out[6];
    r.Write(b, 6);                 // forces a wrap of the write cursor
    r.Read(out, 6);
    assert(memcmp(b, out, 6) == 0);
}

static void test_overrun_drops_oldest() {
    RingRef r(8);
    uint8_t big[12];
    for (int i = 0; i < 12; ++i) big[i] = (uint8_t)i;
    r.Write(big, 12);             // 12 into an 8-byte ring
    assert(r.count() == 8);
    uint8_t out[8];
    r.Read(out, 8);
    // Only the newest 8 bytes (4..11) should survive.
    for (int i = 0; i < 8; ++i) assert(out[i] == (uint8_t)(i + 4));
}

int main() {
    test_basic_roundtrip();
    test_underrun_zero_fills();
    test_wraparound();
    test_overrun_drops_oldest();
    printf("loopbuffer algorithm: all tests passed\n");
    return 0;
}
