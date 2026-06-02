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
    loopback ring, which couples the speaker and microphone endpoints.

Environment:
    Kernel mode.

--*/

#include "minwavecyclic.h"

//=============================================================================
// Format / data-range descriptors (PCM 48k/16/2).
//=============================================================================
#pragma code_seg("PAGE")

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

//=============================================================================
// CMiniportWaveCyclic
//=============================================================================
CMiniportWaveCyclic::CMiniportWaveCyclic(PUNKNOWN other, DISCROD_ROLE role)
    : CUnknown(other), m_role(role), m_adapter(nullptr),
      m_port(nullptr), m_streamAllocated(FALSE)
{
    PAGED_CODE();
}

CMiniportWaveCyclic::~CMiniportWaveCyclic()
{
    PAGED_CODE();
    if (m_port)
    {
        m_port->Release();
        m_port = nullptr;
    }
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::Init(PUNKNOWN unknownAdapter, PRESOURCELIST resourceList,
                         PPORTWAVECYCLIC port)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(resourceList);

    m_port = port;
    m_port->AddRef();

    // The shared loopback ring is created once by the adapter in StartDevice.
    return GetLoopback() ? STATUS_SUCCESS : STATUS_DEVICE_NOT_READY;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclic::GetDescription(PPCFILTER_DESCRIPTOR* outDescriptor)
{
    PAGED_CODE();
    // The full PCFILTER_DESCRIPTOR (pins/nodes/connections) for the render and
    // capture filters is built in the descriptor tables compiled with the WDK
    // (see ARCHITECTURE.md). One data range is shared by both roles.
    ASSERT(outDescriptor);
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
    UNREFERENCED_PARAMETER(outServiceGroup);

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
    return STATUS_SUCCESS;
}

STDMETHODIMP_(void) CMiniportWaveCyclic::PowerChangeNotify(POWER_STATE state)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(state);
}

//=============================================================================
// CMiniportWaveCyclicStream
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

// --- IDmaChannel: PortCls allocates/maps the cyclic buffer through us --------
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
}

// --- IMiniportWaveCyclicStream ---------------------------------------------
STDMETHODIMP_(NTSTATUS) CMiniportWaveCyclicStream::SetFormat(PKSDATAFORMAT format)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(format);
    return STATUS_SUCCESS;   // single fixed format
}

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
    UNREFERENCED_PARAMETER(physicalPosition);
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS)
CMiniportWaveCyclicStream::SetNotificationFreq(ULONG interval, PULONG frameSize)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(interval);
    *frameSize = m_bytesPerInterval;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(void) CMiniportWaveCyclicStream::Silence(PVOID buffer, ULONG count)
{
    RtlZeroMemory(buffer, count);
}
