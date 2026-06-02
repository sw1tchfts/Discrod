# Driver architecture

The Discrod Virtual Audio Cable is a PortCls **WaveCyclic** audio driver that
presents two endpoints to Windows:

```
  App / mic ──► "Discrod Virtual Cable (Speakers)"   [render endpoint]
                          │
                          ▼  CLoopbackBuffer (shared ring)
                          │
  Discord  ◄── "Discrod Virtual Cable (Microphone)"  [capture endpoint]
```

Anything written to the **render** endpoint is copied into a single shared ring
buffer; the **capture** endpoint reads from that same ring. To other apps the
capture endpoint looks exactly like a normal microphone — which is what lets
Discord select it.

## Why WaveCyclic (not WaveRT)

WaveRT exposes a memory-mapped buffer with no per-packet driver callback, so a
loopback has to be driven entirely by position math and DPC timing. WaveCyclic
models a classic DMA engine: the miniport owns the cyclic buffer and a periodic
"DMA" timer, which gives us a natural, explicit place to move samples in and out
of the loopback ring. It is the same model Microsoft's MSVAD virtual-audio
sample uses, and it is far easier to get correct for a first version. We can
migrate to WaveRT later for lower latency.

## Components

| File | Responsibility |
|------|----------------|
| `adapter.cpp` | `DriverEntry`, `AddDevice`, `StartDevice`; installs the render and capture wave + topology subdevices and creates the shared loopback. |
| `loopbuffer.{h,cpp}` | The single-producer/single-consumer ring that couples render → capture. Underrun → silence, overrun → drop oldest. |
| `globals.{h,cpp}` | Owns the one `CLoopbackBuffer` shared by both endpoints. |
| `minwavecyclic.{h,cpp}` | The wave miniport + stream. The stream simulates a DMA engine with a 10 ms timer; each tick moves one interval between the client's cyclic buffer and the loopback ring (`render → Write`, `capture → Read`). |
| `mintopo.{h,cpp}` | Minimal topology miniports so the endpoints surface in the Sound control panel with a volume node. |
| `common.h` | Format (48 kHz/16-bit/stereo), GU​ID/ref strings, pool tag, ring sizing. |

## Data flow per timer tick (10 ms)

```
render stream tick:                capture stream tick:
  client has filled DMA region       ring.Read(dmaRegion, bytes)
  ring.Write(dmaRegion, bytes)       client then reads DMA region
  advance play position              advance record position
```

`m_bytesPerInterval = (avgBytesPerSec / 1000) * 10ms`. The DMA cyclic buffer is
handled with up to two `Write`/`Read` chunks to cover wraparound.

## What is intentionally minimal in v1

- **Descriptor tables.** The full `PCFILTER_DESCRIPTOR` pin/node/connection
  tables for the wave and topology filters are compact and follow the MSVAD
  sample exactly; `GetDescription` is wired but the static tables should be
  filled in alongside the WDK headers (they reference KS GUIDs only available
  there). This is the main remaining on-Windows task.
- **Single fixed format** (48 kHz/16-bit/stereo). Format negotiation returns
  this one range. Add more ranges in `PinDataRangesPCM` / `DataRangeIntersection`.
- **One stream per endpoint** (point-to-point cable). `NewStream` returns
  `STATUS_DEVICE_BUSY` for a second open.
- **Volume node** is declared in topology but not yet applied to samples; the
  app's per-channel DSP currently does the mixing/gain. Hardware-side volume can
  be added in the topology property handlers.

## Verification

`tests/test_loopbuffer.cpp` re-implements the ring's exact index arithmetic and
checks round-trip, wraparound, underrun zero-fill, and overrun drop with a
normal C++ compiler:

```sh
g++ -std=c++17 tests/test_loopbuffer.cpp -o t && ./t
```

Keep it in sync with `loopbuffer.cpp` if you change the ring math.
