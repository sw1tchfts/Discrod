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
| `adapter.cpp` | `DriverEntry`, `AddDevice`, `StartDevice`; installs the render and capture wave + topology subdevices, registers the physical connections between their bridge pins, and creates the shared loopback. |
| `loopbuffer.{h,cpp}` | The single-producer/single-consumer ring that couples render → capture. Underrun → silence, overrun → drop oldest. |
| `globals.{h,cpp}` | Owns the one `CLoopbackBuffer` shared by both endpoints. |
| `minwavecyclic.{h,cpp}` | The wave miniport + stream, plus the render/capture wave `PCFILTER_DESCRIPTOR` tables. The stream simulates a DMA engine with a 10 ms timer; each tick moves one interval between the client's cyclic buffer and the loopback ring (`render → Write`, `capture → Read`) and then notifies the port's service group. |
| `mintopo.{h,cpp}` | Minimal topology miniports + descriptor tables so the endpoints surface in the Sound control panel (speaker / microphone endpoint pins; no volume node yet). |
| `common.h` | Format (48 kHz/16-bit/stereo), GU​ID/ref strings, pool tag, ring sizing. |

## Filter descriptors

`GetDescription` on each miniport returns a static MSVAD-style
`PCFILTER_DESCRIPTOR`; the role chosen at construction (`RoleRender` /
`RoleCapture`) selects which table. All four filters are deliberately
**node-less** in v1 — a streaming/endpoint pin wired straight to a bridge pin:

```
render wave                render topology
  pin 0 (IN, SINK, PCM) ─►  pin 1 (OUT, bridge)
                                 │ physical connection
                                 ▼
                            pin 0 (IN, bridge) ─► pin 1 (OUT, KSNODETYPE_SPEAKER)

capture topology                    capture wave
  pin 1 (IN, KSNODETYPE_MICROPHONE) ─► pin 0 (OUT, bridge)
                                            │ physical connection
                                            ▼
                                       pin 1 (IN, bridge) ─► pin 0 (OUT, SINK, PCM)
```

Key choices (all per MSVAD/SYSVAD conventions):

- **Wave streaming pins** (pin 0): the one PCM data range
  (`PinDataRangesPCM`), `KSPIN_COMMUNICATION_SINK`, max **1** instance
  (point-to-point cable). Render is `KSPIN_DATAFLOW_IN` (client pushes PCM),
  capture is `KSPIN_DATAFLOW_OUT` (client pulls PCM).
- **Bridge pins** (wave pin 1, topology pin 0): analog data range
  (`KSDATAFORMAT_SUBTYPE_ANALOG` / `SPECIFIER_NONE`),
  `KSPIN_COMMUNICATION_NONE`, **0** instances — they carry no IRPs, they only
  describe the wiring.
- **Endpoint pins** (topology pin 1): category `KSNODETYPE_SPEAKER` (render)
  or `KSNODETYPE_MICROPHONE` (capture) — this is what makes Windows surface
  the filters as a speaker and a mic.
- **Filter categories**: explicit per role — wave: `KSCATEGORY_AUDIO` +
  `KSCATEGORY_RENDER` or `KSCATEGORY_CAPTURE`; topology: `KSCATEGORY_AUDIO` +
  `KSCATEGORY_TOPOLOGY` (matches the INF interface registrations).
- `StartDevice` calls `PcRegisterPhysicalConnection` twice to join each wave
  filter's bridge pin to its topology's bridge pin, so the endpoint builder
  sees one continuous path per endpoint.

## Data flow per timer tick (10 ms)

```
render stream tick:                capture stream tick:
  ring.Write(dmaRegion, bytes)       ring.Read(dmaRegion, bytes)
  advance play position              advance record position
  port->Notify(serviceGroup)         port->Notify(serviceGroup)
```

`m_bytesPerInterval = (avgBytesPerSec / 1000) * 10ms`. The DMA cyclic buffer is
handled with up to two `Write`/`Read` chunks to cover wraparound.

### Service group / Notify

PortCls does **not** poll a WaveCyclic miniport: it copies audio between the
client's buffers and the miniport's "DMA" buffer only when the miniport signals
its service group. The flow is:

1. `CMiniportWaveCyclic::Init` creates the group with
   `PcNewServiceGroup(&m_serviceGroup, nullptr)` and stores the
   `IPortWaveCyclic*` (AddRef'd).
2. `NewStream` hands the same group back through `*OutServiceGroup` (AddRef'd —
   PortCls releases its reference; the miniport destructor releases its own).
3. Every timer DPC, after moving one interval through the loopback ring,
   `ServiceLoopback` calls `m_port->Notify(m_serviceGroup)`, which wakes the
   port's service routine to do the client ↔ DMA copies (using our
   `IDmaChannel::CopyTo/CopyFrom` and `GetPosition`).

Without step 3 the timer would tick forever and no audio would move.

## Paging (`#pragma code_seg`) policy

Anything reachable at `DISPATCH_LEVEL` must live in the non-paged default text
section; touching paged code at raised IRQL is a bugcheck. The convention (same
as `loopbuffer.cpp`) is: `#pragma code_seg("PAGE")` + `PAGED_CODE()` for
PASSIVE-only entry points, and an explicit `#pragma code_seg()` before the hot
paths.

- **Non-paged**: the timer DPC (`DmaTimerDpc`), `ServiceLoopback`,
  `AdvancePosition`, `SetState`, `GetPosition`, `NormalizePhysicalPosition`,
  `Silence`, the `IDmaChannel` accessors (`SystemAddress`, `BufferSize`,
  `CopyTo`/`CopyFrom`, …, `GetAdapterObject`), the `CLoopbackBuffer`
  read/write/reset paths, and `GetLoopback()` in `globals.cpp`.
- **PAGE**: `Init`, `GetDescription`, `DataRangeIntersection`, `NewStream`,
  `NonDelegatingQueryInterface`, `SetFormat`, `SetNotificationFreq`,
  `AllocateBuffer`/`FreeBuffer`, constructors/destructors, all of
  `mintopo.cpp`, and the adapter install path.

## What is intentionally minimal in v1

- **Node-less topologies.** The **volume node is deliberately deferred**; the
  app's per-channel DSP currently does the mixing/gain. Adding a
  `KSNODETYPE_VOLUME` node (plus its property handler) between the topology
  bridge and endpoint pins is the natural next step.
- **Single fixed format** (48 kHz/16-bit/stereo). Format negotiation returns
  this one range. Add more ranges in `PinDataRangesPCM` / `DataRangeIntersection`.
- **One stream per endpoint** (point-to-point cable). `NewStream` returns
  `STATUS_DEVICE_BUSY` for a second open, and only the streaming pin
  (`DiscrodWavePinStream`) may be opened.

## What has NOT been verified

This driver has **never been compiled with a real WDK or loaded on Windows**.
The sources follow MSVAD signatures and section placement as closely as careful
reading allows, but descriptor-table field order, GUID/category linkage
(`ksguid.lib`), and the PortCls runtime behavior (service-group timing, position
reporting, endpoint enumeration) all still need on-device validation per
`docs/BUILD.md`. Treat the first Windows build as a porting exercise, not a
formality.

## Verification

`tests/test_loopbuffer.cpp` re-implements the ring's exact index arithmetic and
checks round-trip, wraparound, underrun zero-fill, and overrun drop with a
normal C++ compiler:

```sh
g++ -std=c++17 tests/test_loopbuffer.cpp -o t && ./t
```

Keep it in sync with `loopbuffer.cpp` if you change the ring math.
