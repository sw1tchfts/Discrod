/*++

Module Name:
    minwavecyclic.h

Abstract:
    WaveCyclic miniport for the virtual cable.  A single miniport class serves
    both the render (speaker) and capture (microphone) filters; the role is
    selected at construction.  Each stream simulates a DMA engine with a
    periodic timer (the "virtual hardware") and moves PCM to/from the shared
    loopback ring:

        render  stream:  client buffer --(timer tick)--> loopback.Write()
        capture stream:  loopback.Read() --(timer tick)--> client buffer

    This mirrors Microsoft's MSVAD / WaveCyclic virtual-audio sample structure;
    the loopback wiring is what turns two independent endpoints into a cable.

Environment:
    Kernel mode.

--*/

#ifndef _DISCROD_MINWAVECYCLIC_H_
#define _DISCROD_MINWAVECYCLIC_H_

#include "common.h"
#include "globals.h"

// Timer period for the simulated DMA, in 100ns units (10 ms).
#define DISCROD_TIMER_PERIOD_NS  (10 * 10000)
#define DISCROD_NOTIFY_INTERVAL_MS 10

// Wave filter pin IDs -- indices into the PCPIN_DESCRIPTOR tables in
// minwavecyclic.cpp.  Pin 0 is the streaming pin PortCls opens on behalf of
// audio clients; pin 1 is the bridge pin the adapter physically connects to
// the matching topology filter (PcRegisterPhysicalConnection in adapter.cpp).
enum DISCROD_WAVE_PIN
{
    DiscrodWavePinStream = 0,
    DiscrodWavePinBridge = 1
};

class CMiniportWaveCyclicStream;

//=============================================================================
class CMiniportWaveCyclic : public IMiniportWaveCyclic,
                            public IPowerNotify,
                            public CUnknown
{
public:
    DECLARE_STD_UNKNOWN();
    CMiniportWaveCyclic(PUNKNOWN other, DISCROD_ROLE role);
    ~CMiniportWaveCyclic();

    // IMP_IMiniportWaveCyclic already expands IMP_IMiniport, so this single
    // macro declares GetDescription/DataRangeIntersection/Init/NewStream.
    IMP_IMiniportWaveCyclic;
    STDMETHODIMP_(void) PowerChangeNotify(POWER_STATE state);

    DISCROD_ROLE Role() const { return m_role; }

private:
    DISCROD_ROLE  m_role;
    PPORTWAVECYCLIC m_port;          // stored (AddRef'd) in Init
    PSERVICEGROUP m_serviceGroup;    // created in Init; signalled from the DPC
    BOOLEAN       m_streamAllocated; // single stream per pin for the cable

    friend class CMiniportWaveCyclicStream;
};

//=============================================================================
class CMiniportWaveCyclicStream : public IMiniportWaveCyclicStream,
                                  public IDmaChannel,
                                  public CUnknown
{
public:
    DECLARE_STD_UNKNOWN();
    CMiniportWaveCyclicStream(PUNKNOWN other);
    ~CMiniportWaveCyclicStream();

    IMP_IMiniportWaveCyclicStream;     // SetFormat, SetState, GetPosition, ...
    IMP_IDmaChannel;                   // AllocateBuffer, TransferCount, ...

    NTSTATUS Init(_In_ CMiniportWaveCyclic* miniport,
                  _In_ ULONG channel,
                  _In_ BOOLEAN capture,
                  _In_ PKSDATAFORMAT format);

    // Called from the DMA timer DPC to move one tick of audio.
    void ServiceLoopback();

private:
    CMiniportWaveCyclic* m_miniport;
    BOOLEAN     m_capture;
    KSSTATE     m_state;

    // Simulated DMA cyclic buffer (the "hardware" buffer PortCls maps).
    PVOID       m_dmaBuffer;
    ULONG       m_dmaBufferSize;
    ULONG       m_dmaPosition;          // byte offset, wraps at buffer size
    ULONGLONG   m_bytesTransferred;     // monotonic, for GetPosition

    // Periodic "DMA" timer.
    KTIMER      m_timer;
    KDPC        m_dpc;
    ULONG       m_bytesPerInterval;     // bytes per timer tick at the format rate

    void AdvancePosition(ULONG bytes);
};

#endif // _DISCROD_MINWAVECYCLIC_H_
