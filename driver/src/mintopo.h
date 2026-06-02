/*++

Module Name:
    mintopo.h

Abstract:
    Minimal topology miniport for the virtual cable endpoints.  The topology
    filter exposes the endpoint to the Windows audio system (so it shows up in
    the Sound control panel and in Discord's device list) and carries the volume
    node.  Render and capture each get a topology paired with their wave filter.

Environment:
    Kernel mode.

--*/

#ifndef _DISCROD_MINTOPO_H_
#define _DISCROD_MINTOPO_H_

#include "common.h"

class CMiniportTopology : public IMiniportTopology, public CUnknown
{
public:
    DECLARE_STD_UNKNOWN();
    CMiniportTopology(PUNKNOWN other, DISCROD_ROLE role);
    ~CMiniportTopology();

    IMP_IMiniportTopology;   // Init, GetDescription, DataRangeIntersection
    IMP_IMiniport;

private:
    DISCROD_ROLE m_role;
};

// Factory used by the adapter when installing the topology subdevice.
NTSTATUS CreateMiniportTopology(_Out_ PUNKNOWN* out, _In_ REFCLSID,
                                _In_opt_ PUNKNOWN outer, _In_ POOL_TYPE pool,
                                _In_ DISCROD_ROLE role);

#endif // _DISCROD_MINTOPO_H_
