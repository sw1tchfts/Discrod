/*++

Module Name:
    minwavecyclic.cpp

Abstract:
    Implementation of the WaveCyclic miniport and its stream for the virtual
    cable.  See minwavecyclic.h for the design overview.

    The miniport advertises a single PCM data range (48 kHz / 16-bit / stereo).
    Each stream allocates a cyclic "DMA" buffer that PortCls maps for the client,
    and a 10 ms periodic timer simulates the hardware DMA pointer.  On every tick
    we move one interval of audio between the client's DMA buffer and the shared
    loopback ring (which couples the speaker and microphone endpoints), then
    signal the port's service group so PortCls performs the client<->DMA copies.

    Paging policy (see also ARCHITECTURE.md):
      - PAGE section: everything PortCls calls at PASSIVE_LEVEL only (Init,
        GetDescription, DataRangeIntersection, NewStream, QI, SetFormat,
        SetNotificationFreq, AllocateBuffer, FreeBuffer, ctors/dtors).
      - default (non-paged) section: everything reachable at DISPATCH_LEVEL --
        the timer DPC, ServiceLoopback, AdvancePosition, SetState, GetPosition,
        NormalizePhysicalPosition, Silence, and all IDmaChannel accessors.

Environment:
    Kernel mode.

--*/

#include "minwavecyclic.h"

//=============================================================================
// Static descriptor tables (MSVAD-style).  These are data, not code, so they
// are unaffected by #pragma code_seg; PortCls walks them at PASSIVE_LEVEL.
//=============================================================================

// The one PCM format of the cable (48 kHz / 16-bit / stereo).
static KSDATARANGE_AUDIO PinDataRangesPCM =
{
    {
        sizeof(KSDATARANGE_AUDIO),
        0,
        0,
        0,
        STATICGUIDOF(KSDATAFORMAT_TYPE_AUDIO),
        STATICGUIDOF(KSDATAFORMAT_SUBTYPE_PCM),
        STATICGUIDOF(KSDATAFORMAT_SPECIFIER_WAVEFORMATEX)
    },
    DISCROD_CHANNELS,
    DISCROD_BITS_PER_SAMPLE,
    DISCROD_BITS_PER_SAMPLE,
    DISCROD_SAMPLE_RATE,
    DISCROD_SAMPLE_RATE
};

static PKSDATARANGE PinDataRangePointers[] =
{
    PKSDATARANGE(&PinDataRangesPCM)
};

// Bridge pins carry no wave format -- analog "wire" to the topology filter.
static KSDATARANGE PinDataRangesBridge[] =
{
    {
        sizeof(KSDATARANGE),
        0,
        0,
        0,
        STATICGUIDOF(KSDATAFORMAT_TYPE_AUDIO),
        STATICGUIDOF(KSDATAFORMAT_SUBTYPE_ANALOG),
        STATICGUIDOF(KSDATAFORMAT_SPECIFIER_NONE)
    }
};

static PKSDATARANGE PinDataRangePointersBridge[] =
{
    &PinDataRangesBridge[0]
};

//-----------------------------------------------------------------------------
// Render wave filter: client renders into pin 0 (sink), audio leaves through
// the bridge pin 1 toward the render topology (and, via the loopback ring,
// toward the capture side).
//-----------------------------------------------------------------------------
static PCPIN_DESCRIPTOR RenderWavePins[] =
{
    // DiscrodWavePinStream: the system streaming pin (WaveOut sink).
    {
        1, 1, 0,                                // one instance: point-to-point
        NULL,                                   // AutomationTable
        {
            0, NULL,                            // Interfaces
            0, NULL,                            // Mediums
            SIZEOF_ARRAY(PinDataRangePointers), // DataRangesCount
            PinDataRangePointers,               // DataRanges
            KSPIN_DATAFLOW_IN,                  // client pushes PCM in
            KSPIN_COMMUNICATION_SINK,           // accepts connection IRPs
            &KSCATEGORY_AUDIO,                  // Category
            NULL,                               // Name
            0                                   // Reserved
        }
    },
    // DiscrodWavePinBridge: physical connection to the render topology.
    {
        0, 0, 0,                                // bridge pins take no instances
        NULL,
        {
            0, NULL,
            0, NULL,
            SIZEOF_ARRAY(PinDataRangePointersBridge),
            PinDataRangePointersBridge,
            KSPIN_DATAFLOW_OUT,
            KSPIN_COMMUNICATION_NONE,           // no IRPs on a bridge pin
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    }
};

// Node-less filter: the streaming pin feeds the bridge pin directly.
static PCCONNECTION_DESCRIPTOR RenderWaveConnections[] =
{
    { PCFILTER_NODE, DiscrodWavePinStream, PCFILTER_NODE, DiscrodWavePinBridge }
};

static GUID RenderWaveCategories[] =
{
    { STATICGUIDOF(KSCATEGORY_AUDIO) },
    { STATICGUIDOF(KSCATEGORY_RENDER) }
};

static PCFILTER_DESCRIPTOR RenderWaveFilterDescriptor =
{
    0,                                   // Version
    NULL,                                // AutomationTable
    sizeof(PCPIN_DESCRIPTOR),            // PinSize
    SIZEOF_ARRAY(RenderWavePins),        // PinCount
    RenderWavePins,                      // Pins
    sizeof(PCNODE_DESCRIPTOR),           // NodeSize
    0,                                   // NodeCount
    NULL,                                // Nodes
    SIZEOF_ARRAY(RenderWaveConnections), // ConnectionCount
    RenderWaveConnections,               // Connections
    SIZEOF_ARRAY(RenderWaveCategories),  // CategoryCount
    RenderWaveCategories                 // Categories
};

//-----------------------------------------------------------------------------
// Capture wave filter: audio arrives on bridge pin 1 (from the capture
// topology / the loopback ring) and the client records from pin 0 (source
// dataflow, IRP sink).
//-----------------------------------------------------------------------------
static PCPIN_DESCRIPTOR CaptureWavePins[] =
{
    // DiscrodWavePinStream: the system streaming pin (WaveIn).
    {
        1, 1, 0,
        NULL,
        {
            0, NULL,
            0, NULL,
            SIZEOF_ARRAY(PinDataRangePointers),
            PinDataRangePointers,
            KSPIN_DATAFLOW_OUT,                 // client pulls PCM out
            KSPIN_COMMUNICATION_SINK,           // still the IRP sink
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    },
    // DiscrodWavePinBridge: physical connection from the capture topology.
    {
        0, 0, 0,
        NULL,
        {
            0, NULL,
            0, NULL,
            SIZEOF_ARRAY(PinDataRangePointersBridge),
            PinDataRangePointersBridge,
            KSPIN_DATAFLOW_IN,
            KSPIN_COMMUNICATION_NONE,
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    }
};

static PCCONNECTION_DESCRIPTOR CaptureWaveConnections[] =
{
    { PCFILTER_NODE, DiscrodWavePinBridge, PCFILTER_NODE, DiscrodWavePinStream }
};

static GUID CaptureWaveCategories[] =
{
    { STATICGUIDOF(KSCATEGORY_AUDIO) },
    { STATICGUIDOF(KSCATEGORY_CAPTURE) }
};

static PCFILTER_DESCRIPTOR CaptureWaveFilterDescriptor =
{
    0,
    NULL,
    sizeof(PCPIN_DESCRIPTOR),
    SIZEOF_ARRAY(CaptureWavePins),
    CaptureWavePins,
    sizeof(PCNODE_DESCRIPTOR),
    0,
    NULL,
    SIZEOF_ARRAY(CaptureWaveConnections),
    CaptureWaveConnections,
    SIZEOF_ARRAY(CaptureWaveCategories),
    CaptureWaveCategories
};

// DPC trampoline (defined in the non-paged section below; declared here so
// stream Init can wire it into KeInitializeDpc).
static void DmaTimerDpc(PKDPC dpc, PVOID context, PVOID a1, PVOID a2);

//=============================================================================
// CMiniportWaveCyclic -- PASSIVE_LEVEL-only methods (pageable).
//=============================================================================
#pragma code_seg("PAGE")

CMiniportWaveCyclic::CMiniportWaveCyclic(PUNKNOWN other, DISCROD_ROLE role)
    : CUnknown(other), m_role(role), m_port(nullptr),
      m_serviceGroup(nullptr), m_streamAllocated(FALSE)
{
    PAGED_CODE();
}

CMiniportWaveCyclic::~CMiniportWaveCyclic()
{
    PAGED_CODE();
    if (m_serviceGroup)
    {
        m_serviceGroup->Release();
        m_serviceGroup = nullptr;
    }
    if (m_port)
    {
        m_port->Release();
        m_port = nullptr;
    }
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::NonDelegatingQueryInterface(REFIID interfaceId,
                                                PVOID* object)
{
    PAGED_CODE();
    ASSERT(object);

    if (IsEqualGUIDAligned(interfaceId, IID_IUnknown))
    {
        *object = PVOID(PUNKNOWN(PMINIPORTWAVECYCLIC(this)));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IMiniport))
    {
        *object = PVOID(PMINIPORT(this));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IMiniportWaveCyclic))
    {
        *object = PVOID(PMINIPORTWAVECYCLIC(this));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IPowerNotify))
    {
        *object = PVOID(PPOWERNOTIFY(this));
    }
    else
    {
        *object = nullptr;
    }

    if (*object)
    {
        // AddRef through the interface we are handing out (MSVAD pattern).
        PUNKNOWN(*object)->AddRef();
        return STATUS_SUCCESS;
    }

    return STATUS_INVALID_PARAMETER;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::Init(PUNKNOWN unknownAdapter, PRESOURCELIST resourceList,
                         PPORTWAVECYCLIC port)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(unknownAdapter);
    UNREFERENCED_PARAMETER(resourceList);

    m_port = port;
    m_port->AddRef();

    // The service group is how the "virtual DMA" tells PortCls to service the
    // stream: the timer DPC calls m_port->Notify(m_serviceGroup), which drives
    // the port's client-buffer <-> DMA-buffer copies.  Without it no audio
    // would ever move, even with the timer running.
    NTSTATUS status = PcNewServiceGroup(&m_serviceGroup, nullptr);
    if (!NT_SUCCESS(status))
    {
        return status;
    }

    // The shared loopback ring is created once by the adapter in StartDevice.
    return GetLoopback() ? STATUS_SUCCESS : STATUS_DEVICE_NOT_READY;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::GetDescription(PPCFILTER_DESCRIPTOR* outDescriptor)
{
    PAGED_CODE();
    ASSERT(outDescriptor);

    // One miniport class, two filter shapes: the role chosen at construction
    // selects the render or capture descriptor table above.
    *outDescriptor = (m_role == RoleCapture) ? &CaptureWaveFilterDescriptor
                                             : &RenderWaveFilterDescriptor;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::DataRangeIntersection(ULONG pinId, PKSDATARANGE clientRange,
                                           PKSDATARANGE myRange,
                                           ULONG outputBufferLength,
                                           PVOID resultantFormat,
                                           PULONG resultantFormatLength)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(pinId);
    UNREFERENCED_PARAMETER(clientRange);
    UNREFERENCED_PARAMETER(myRange);

    // We expose a single fixed PCM format, so the intersection is simply that
    // WAVEFORMATEX when the buffer is large enough.
    ULONG required = sizeof(KSDATAFORMAT) + sizeof(WAVEFORMATEX);
    if (outputBufferLength == 0)
    {
        *resultantFormatLength = required;
        return STATUS_BUFFER_OVERFLOW;
    }
    if (outputBufferLength < required)
    {
        return STATUS_BUFFER_TOO_SMALL;
    }

    PKSDATAFORMAT_WAVEFORMATEX fmt =
        static_cast<PKSDATAFORMAT_WAVEFORMATEX>(resultantFormat);
    RtlZeroMemory(fmt, required);
    fmt->DataFormat.FormatSize = required;
    fmt->DataFormat.MajorFormat = KSDATAFORMAT_TYPE_AUDIO;
    fmt->DataFormat.SubFormat = KSDATAFORMAT_SUBTYPE_PCM;
    fmt->DataFormat.Specifier = KSDATAFORMAT_SPECIFIER_WAVEFORMATEX;
    fmt->WaveFormatEx.wFormatTag = WAVE_FORMAT_PCM;
    fmt->WaveFormatEx.nChannels = DISCROD_CHANNELS;
    fmt->WaveFormatEx.nSamplesPerSec = DISCROD_SAMPLE_RATE;
    fmt->WaveFormatEx.wBitsPerSample = DISCROD_BITS_PER_SAMPLE;
    fmt->WaveFormatEx.nBlockAlign = DISCROD_BLOCK_ALIGN;
    fmt->WaveFormatEx.nAvgBytesPerSec = DISCROD_AVG_BYTES_PER_SEC;
    fmt->WaveFormatEx.cbSize = 0;
    *resultantFormatLength = required;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::NewStream(PMINIPORTWAVECYCLICSTREAM* outStream,
                              PUNKNOWN outer, POOL_TYPE poolType,
                              ULONG pin, BOOLEAN capture,
                              PKSDATAFORMAT format,
                              PDMACHANNEL* outDmaChannel,
                              PSERVICEGROUP* outServiceGroup)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(poolType);

    if (pin != DiscrodWavePinStream)
    {
        // Only the streaming pin can be opened; the bridge pin carries no IRPs.
        return STATUS_INVALID_PARAMETER;
    }

    if (m_streamAllocated)
    {
        // The cable is point-to-point: one stream per endpoint.
        return STATUS_DEVICE_BUSY;
    }

    CMiniportWaveCyclicStream* stream =
        new (POOL_FLAG_NON_PAGED, DISCROD_POOLTAG)
            CMiniportWaveCyclicStream(outer);
    if (stream == nullptr)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    stream->AddRef();

    NTSTATUS status = stream->Init(this, pin, capture, format);
    if (!NT_SUCCESS(status))
    {
        stream->Release();
        return status;
    }

    m_streamAllocated = TRUE;
    *outStream = static_cast<PMINIPORTWAVECYCLICSTREAM>(stream);
    *outDmaChannel = static_cast<PDMACHANNEL>(stream);

    // Hand PortCls the service group our timer DPC notifies; PortCls only
    // moves audio between the client buffer and our DMA buffer when this
    // group is signalled (see ServiceLoopback).
    *outServiceGroup = m_serviceGroup;
    m_serviceGroup->AddRef();

    return STATUS_SUCCESS;
}

STDMETHODIMP_(void) CMiniportWaveCyclic::PowerChangeNotify(POWER_STATE state)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(state);
}

//=============================================================================
// CMiniportWaveCyclicStream -- PASSIVE_LEVEL-only methods (pageable).
//=============================================================================

CMiniportWaveCyclicStream::CMiniportWaveCyclicStream(PUNKNOWN other)
    : CUnknown(other), m_miniport(nullptr), m_capture(FALSE),
      m_state(KSSTATE_STOP), m_dmaBuffer(nullptr), m_dmaBufferSize(0),
      m_dmaPosition(0), m_bytesTransferred(0), m_bytesPerInterval(0)
{
    PAGED_CODE();
}

CMiniportWaveCyclicStream::~CMiniportWaveCyclicStream()
{
    PAGED_CODE();
    KeCancelTimer(&m_timer);
    if (m_dmaBuffer)
    {
        ExFreePoolWithTag(m_dmaBuffer, DISCROD_POOLTAG);
        m_dmaBuffer = nullptr;
    }
    if (m_miniport)
    {
        m_miniport->m_streamAllocated = FALSE;
    }
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclicStream::NonDelegatingQueryInterface(REFIID interfaceId,
                                                       PVOID* object)
{
    PAGED_CODE();
    ASSERT(object);

    if (IsEqualGUIDAligned(interfaceId, IID_IUnknown))
    {
        *object = PVOID(PUNKNOWN(PMINIPORTWAVECYCLICSTREAM(this)));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IMiniportWaveCyclicStream))
    {
        *object = PVOID(PMINIPORTWAVECYCLICSTREAM(this));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IDmaChannel))
    {
        *object = PVOID(PDMACHANNEL(this));
    }
    else
    {
        *object = nullptr;
    }

    if (*object)
    {
        PUNKNOWN(*object)->AddRef();
        return STATUS_SUCCESS;
    }

    return STATUS_INVALID_PARAMETER;
}

NTSTATUS CMiniportWaveCyclicStream::Init(CMiniportWaveCyclic* miniport,
                                         ULONG channel, BOOLEAN capture,
                                         PKSDATAFORMAT format)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(channel);
    UNREFERENCED_PARAMETER(format);

    m_miniport = miniport;
    m_capture = capture;
    m_bytesPerInterval =
        (DISCROD_AVG_BYTES_PER_SEC / 1000) * DISCROD_NOTIFY_INTERVAL_MS;

    KeInitializeDpc(&m_dpc, DmaTimerDpc, this);
    KeInitializeTimerEx(&m_timer, SynchronizationTimer);
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS) CMiniportWaveCyclicStream::SetFormat(PKSDATAFORMAT format)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(format);
    return STATUS_SUCCESS;   // single fixed format
}

STDMETHODIMP_(ULONG)
CMiniportWaveCyclicStream::SetNotificationFreq(ULONG interval, PULONG frameSize)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(interval);

    // The virtual DMA runs at a fixed 10 ms granularity regardless of the
    // requested interval; report the actual interval back (per the
    // IMiniportWaveCyclicStream contract this returns the interval in ms).
    *frameSize = m_bytesPerInterval;
    return DISCROD_NOTIFY_INTERVAL_MS;
}

// --- IDmaChannel: buffer setup/teardown runs at PASSIVE_LEVEL ---------------
STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclicStream::AllocateBuffer(ULONG bufferSize,
                                          PPHYSICAL_ADDRESS physAddr)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(physAddr);

    m_dmaBuffer = ExAllocatePool2(POOL_FLAG_NON_PAGED, bufferSize,
                                  DISCROD_POOLTAG);
    if (m_dmaBuffer == nullptr)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    m_dmaBufferSize = bufferSize;
    m_dmaPosition = 0;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(void) CMiniportWaveCyclicStream::FreeBuffer()
{
    PAGED_CODE();
    if (m_dmaBuffer)
    {
        ExFreePoolWithTag(m_dmaBuffer, DISCROD_POOLTAG);
        m_dmaBuffer = nullptr;
        m_dmaBufferSize = 0;
    }
}

//=============================================================================
// DISPATCH_LEVEL paths -- everything below here must be non-paged.  The timer
// DPC lands here, and PortCls calls the position/copy/state entry points at
// DISPATCH_LEVEL while the stream runs.
//=============================================================================
#pragma code_seg()

// DPC trampoline: invoked every timer period to service the simulated DMA.
static void DmaTimerDpc(PKDPC dpc, PVOID context, PVOID a1, PVOID a2)
{
    UNREFERENCED_PARAMETER(dpc);
    UNREFERENCED_PARAMETER(a1);
    UNREFERENCED_PARAMETER(a2);
    auto* stream = static_cast<CMiniportWaveCyclicStream*>(context);
    if (stream)
    {
        stream->ServiceLoopback();
    }
}

// --- IDmaChannel accessors: PortCls uses these during service at DISPATCH ---
STDMETHODIMP_(PVOID) CMiniportWaveCyclicStream::SystemAddress()
{
    return m_dmaBuffer;
}

STDMETHODIMP_(ULONG) CMiniportWaveCyclicStream::AllocatedBufferSize()
{
    return m_dmaBufferSize;
}

STDMETHODIMP_(ULONG) CMiniportWaveCyclicStream::BufferSize()
{
    return m_dmaBufferSize;
}

STDMETHODIMP_(void) CMiniportWaveCyclicStream::SetBufferSize(ULONG size)
{
    m_dmaBufferSize = size;
}

STDMETHODIMP_(ULONG) CMiniportWaveCyclicStream::TransferCount()
{
    return m_dmaBufferSize;
}

STDMETHODIMP_(ULONG) CMiniportWaveCyclicStream::MaximumBufferSize()
{
    return DISCROD_RING_BYTES;
}

STDMETHODIMP_(PHYSICAL_ADDRESS) CMiniportWaveCyclicStream::PhysicalAddress()
{
    PHYSICAL_ADDRESS pa;
    pa.QuadPart = 0;   // virtual device: no real physical DMA address
    return pa;
}

STDMETHODIMP_(PADAPTER_OBJECT) CMiniportWaveCyclicStream::GetAdapterObject()
{
    // Virtual device: there is no system DMA adapter behind this channel.
    return nullptr;
}

STDMETHODIMP_(void) CMiniportWaveCyclicStream::CopyTo(PVOID dst, PVOID src, ULONG count)
{
    RtlCopyMemory(dst, src, count);
}

STDMETHODIMP_(void) CMiniportWaveCyclicStream::CopyFrom(PVOID dst, PVOID src, ULONG count)
{
    RtlCopyMemory(dst, src, count);
}

// --- The simulated DMA service routine --------------------------------------
void CMiniportWaveCyclicStream::AdvancePosition(ULONG bytes)
{
    if (m_dmaBufferSize == 0)
    {
        return;
    }
    m_dmaPosition = (m_dmaPosition + bytes) % m_dmaBufferSize;
    m_bytesTransferred += bytes;
}

void CMiniportWaveCyclicStream::ServiceLoopback()
{
    if (m_state != KSSTATE_RUN || m_dmaBuffer == nullptr)
    {
        return;
    }

    CLoopbackBuffer* ring = GetLoopback();
    if (ring == nullptr)
    {
        return;
    }

    ULONG bytes = m_bytesPerInterval;
    ULONG pos = m_dmaPosition;

    // Handle wrap of the cyclic DMA buffer in up to two chunks.
    ULONG firstChunk = min(bytes, m_dmaBufferSize - pos);
    PUCHAR p = static_cast<PUCHAR>(m_dmaBuffer) + pos;

    if (m_capture)
    {
        // Pull from the cable into the client's capture buffer.
        ring->Read(p, firstChunk);
        if (bytes > firstChunk)
        {
            ring->Read(m_dmaBuffer, bytes - firstChunk);
        }
    }
    else
    {
        // Push what the client just rendered into the cable.
        ring->Write(p, firstChunk);
        if (bytes > firstChunk)
        {
            ring->Write(m_dmaBuffer, bytes - firstChunk);
        }
    }

    AdvancePosition(bytes);

    // Signal the service group so PortCls performs the client<->DMA copies
    // for this interval.  Without this Notify no audio ever reaches (or
    // leaves) the client buffers, no matter how busily the timer ticks.
    if (m_miniport != nullptr && m_miniport->m_port != nullptr &&
        m_miniport->m_serviceGroup != nullptr)
    {
        m_miniport->m_port->Notify(m_miniport->m_serviceGroup);
    }
}

// --- IMiniportWaveCyclicStream (DISPATCH-capable entry points) ---------------
STDMETHODIMP_(NTSTATUS) CMiniportWaveCyclicStream::SetState(KSSTATE newState)
{
    if (newState == m_state)
    {
        return STATUS_SUCCESS;
    }

    if (newState == KSSTATE_RUN)
    {
        LARGE_INTEGER due;
        due.QuadPart = -(LONGLONG)DISCROD_TIMER_PERIOD_NS;
        KeSetTimerEx(&m_timer, due, DISCROD_NOTIFY_INTERVAL_MS, &m_dpc);
    }
    else
    {
        KeCancelTimer(&m_timer);
        if (newState == KSSTATE_STOP)
        {
            m_dmaPosition = 0;
            m_bytesTransferred = 0;
            // Reset the ring when the producer (render) stops to avoid stale
            // audio leaking into the mic on the next session.
            if (!m_capture && GetLoopback())
            {
                GetLoopback()->Reset();
            }
        }
    }

    m_state = newState;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS) CMiniportWaveCyclicStream::GetPosition(PULONG position)
{
    // Position within the cyclic buffer that the "hardware" has reached.
    *position = m_dmaPosition;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclicStream::NormalizePhysicalPosition(PLONGLONG physicalPosition)
{
    // Convert a byte count into time in 100-nanosecond units at the fixed
    // engine format (MSVAD formula for a constant byte rate).
    *physicalPosition =
        (*physicalPosition * 10000000LL) / DISCROD_AVG_BYTES_PER_SEC;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(void) CMiniportWaveCyclicStream::Silence(PVOID buffer, ULONG count)
{
    RtlZeroMemory(buffer, count);
}
