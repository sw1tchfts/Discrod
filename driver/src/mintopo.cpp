/*++

Module Name:
    mintopo.cpp

Abstract:
    Minimal topology miniport implementation.  Each topology filter is a
    node-less "wire": a bridge pin toward the wave filter connected straight
    to an endpoint pin (KSNODETYPE_SPEAKER for render, KSNODETYPE_MICROPHONE
    for capture).  The endpoint pin category is what makes Windows surface the
    filter as a speaker / microphone device.  A volume node is deliberately
    deferred (the app does gain in software); see ARCHITECTURE.md.

    All topology entry points run at PASSIVE_LEVEL, so the whole file is
    pageable.

Environment:
    Kernel mode.

--*/

#include "mintopo.h"

//=============================================================================
// Static descriptor tables (MSVAD-style).  Data, not code: unaffected by
// #pragma code_seg; PortCls walks them at PASSIVE_LEVEL.
//=============================================================================

// Topology pins carry no wave format -- they model the analog path.
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
// Render topology: audio flows in from the render wave filter's bridge pin
// and out of the speaker endpoint pin.
//-----------------------------------------------------------------------------
static PCPIN_DESCRIPTOR RenderTopoPins[] =
{
    // DiscrodTopoPinWaveBridge: physical connection from the render wave filter.
    {
        0, 0, 0,                                // bridge pins take no instances
        NULL,                                   // AutomationTable
        {
            0, NULL,                            // Interfaces
            0, NULL,                            // Mediums
            SIZEOF_ARRAY(PinDataRangePointersBridge),
            PinDataRangePointersBridge,
            KSPIN_DATAFLOW_IN,
            KSPIN_COMMUNICATION_NONE,           // no IRPs on a bridge pin
            &KSCATEGORY_AUDIO,                  // Category
            NULL,                               // Name
            0                                   // Reserved
        }
    },
    // DiscrodTopoPinEndpoint: the (virtual) speaker jack.
    {
        0, 0, 0,
        NULL,
        {
            0, NULL,
            0, NULL,
            SIZEOF_ARRAY(PinDataRangePointersBridge),
            PinDataRangePointersBridge,
            KSPIN_DATAFLOW_OUT,
            KSPIN_COMMUNICATION_NONE,
            &KSNODETYPE_SPEAKER,                // endpoint type = speaker
            NULL,
            0
        }
    }
};

// Node-less wire: wave bridge -> speaker endpoint.
static PCCONNECTION_DESCRIPTOR RenderTopoConnections[] =
{
    { PCFILTER_NODE, DiscrodTopoPinWaveBridge,
      PCFILTER_NODE, DiscrodTopoPinEndpoint }
};

static GUID TopoCategories[] =
{
    { STATICGUIDOF(KSCATEGORY_AUDIO) },
    { STATICGUIDOF(KSCATEGORY_TOPOLOGY) }
};

static PCFILTER_DESCRIPTOR RenderTopoFilterDescriptor =
{
    0,                                   // Version
    NULL,                                // AutomationTable
    sizeof(PCPIN_DESCRIPTOR),            // PinSize
    SIZEOF_ARRAY(RenderTopoPins),        // PinCount
    RenderTopoPins,                      // Pins
    sizeof(PCNODE_DESCRIPTOR),           // NodeSize
    0,                                   // NodeCount
    NULL,                                // Nodes
    SIZEOF_ARRAY(RenderTopoConnections), // ConnectionCount
    RenderTopoConnections,               // Connections
    SIZEOF_ARRAY(TopoCategories),        // CategoryCount
    TopoCategories                       // Categories
};

//-----------------------------------------------------------------------------
// Capture topology: audio flows in from the microphone endpoint pin and out
// of the bridge pin toward the capture wave filter.
//-----------------------------------------------------------------------------
static PCPIN_DESCRIPTOR CaptureTopoPins[] =
{
    // DiscrodTopoPinWaveBridge: physical connection to the capture wave filter.
    {
        0, 0, 0,
        NULL,
        {
            0, NULL,
            0, NULL,
            SIZEOF_ARRAY(PinDataRangePointersBridge),
            PinDataRangePointersBridge,
            KSPIN_DATAFLOW_OUT,
            KSPIN_COMMUNICATION_NONE,
            &KSCATEGORY_AUDIO,
            NULL,
            0
        }
    },
    // DiscrodTopoPinEndpoint: the (virtual) microphone jack.
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
            &KSNODETYPE_MICROPHONE,             // endpoint type = microphone
            NULL,
            0
        }
    }
};

// Node-less wire: microphone endpoint -> wave bridge.
static PCCONNECTION_DESCRIPTOR CaptureTopoConnections[] =
{
    { PCFILTER_NODE, DiscrodTopoPinEndpoint,
      PCFILTER_NODE, DiscrodTopoPinWaveBridge }
};

static PCFILTER_DESCRIPTOR CaptureTopoFilterDescriptor =
{
    0,
    NULL,
    sizeof(PCPIN_DESCRIPTOR),
    SIZEOF_ARRAY(CaptureTopoPins),
    CaptureTopoPins,
    sizeof(PCNODE_DESCRIPTOR),
    0,
    NULL,
    SIZEOF_ARRAY(CaptureTopoConnections),
    CaptureTopoConnections,
    SIZEOF_ARRAY(TopoCategories),
    TopoCategories
};

//=============================================================================
// CMiniportTopology -- all PASSIVE_LEVEL, all pageable.
//=============================================================================
#pragma code_seg("PAGE")

CMiniportTopology::CMiniportTopology(PUNKNOWN other, DISCROD_ROLE role)
    : CUnknown(other), m_role(role)
{
    PAGED_CODE();
}

CMiniportTopology::~CMiniportTopology()
{
    PAGED_CODE();
}

STDMETHODIMP_(NTSTATUS)
CMiniportTopology::NonDelegatingQueryInterface(REFIID interfaceId,
                                              PVOID* object)
{
    PAGED_CODE();
    ASSERT(object);

    if (IsEqualGUIDAligned(interfaceId, IID_IUnknown))
    {
        *object = PVOID(PUNKNOWN(PMINIPORTTOPOLOGY(this)));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IMiniport))
    {
        *object = PVOID(PMINIPORT(this));
    }
    else if (IsEqualGUIDAligned(interfaceId, IID_IMiniportTopology))
    {
        *object = PVOID(PMINIPORTTOPOLOGY(this));
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
CMiniportTopology::Init(PUNKNOWN unknownAdapter, PRESOURCELIST resourceList,
                       PPORTTOPOLOGY port)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(unknownAdapter);
    UNREFERENCED_PARAMETER(resourceList);
    UNREFERENCED_PARAMETER(port);
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS)
CMiniportTopology::GetDescription(PPCFILTER_DESCRIPTOR* outDescriptor)
{
    PAGED_CODE();
    ASSERT(outDescriptor);

    // Role selects the render (speaker) or capture (microphone) table above.
    *outDescriptor = (m_role == RoleCapture) ? &CaptureTopoFilterDescriptor
                                             : &RenderTopoFilterDescriptor;
    return STATUS_SUCCESS;
}

STDMETHODIMP_(NTSTATUS)
CMiniportTopology::DataRangeIntersection(ULONG pinId, PKSDATARANGE clientRange,
                                         PKSDATARANGE myRange,
                                         ULONG outputBufferLength,
                                         PVOID resultantFormat,
                                         PULONG resultantFormatLength)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(pinId);
    UNREFERENCED_PARAMETER(clientRange);
    UNREFERENCED_PARAMETER(myRange);
    UNREFERENCED_PARAMETER(outputBufferLength);
    UNREFERENCED_PARAMETER(resultantFormat);
    UNREFERENCED_PARAMETER(resultantFormatLength);
    // Topology bridge pins carry no data format.
    return STATUS_NOT_IMPLEMENTED;
}

NTSTATUS CreateMiniportTopology(PUNKNOWN* out, REFCLSID, PUNKNOWN outer,
                                POOL_TYPE pool, DISCROD_ROLE role)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(pool);

    CMiniportTopology* obj =
        new (POOL_FLAG_NON_PAGED, DISCROD_POOLTAG)
            CMiniportTopology(outer, role);
    if (obj == nullptr)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    *out = static_cast<PUNKNOWN>(static_cast<IMiniportTopology*>(obj));
    (*out)->AddRef();
    return STATUS_SUCCESS;
}
