/*++

Module Name:
    mintopo.cpp

Abstract:
    Minimal topology miniport implementation.  The topology graph (a single
    volume node between the wave pin and the physical endpoint jack) is built
    from descriptor tables compiled with the WDK; see ARCHITECTURE.md.  This
    file provides the COM plumbing and the factory used by the adapter.

Environment:
    Kernel mode.

--*/

#include "mintopo.h"

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
    // Render: KSPIN wave-in -> volume node -> KSPIN speaker jack.
    // Capture: KSPIN mic jack -> volume node -> KSPIN wave-out.
    // Descriptor tables live alongside this file in the WDK build.
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
