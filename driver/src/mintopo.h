/*++

Module Name:
    mintopo.h

Abstract:
    Minimal topology miniport for the virtual cable endpoints.  The topology
    filter exposes the endpoint to the Windows audio system (so it shows up in
    the Sound control panel and in Discord's device list).  Render and capture
    each get a topology paired with their wave filter; v1 is a node-less
    bridge-to-endpoint connection (the volume node is deliberately deferred,
    see ARCHITECTURE.md).

Environment:
    Kernel mode.

--*/

#ifndef _DISCROD_MINTOPO_H_
#define _DISCROD_MINTOPO_H_

#include "common.h"

// Topology filter pin IDs -- indices into the PCPIN_DESCRIPTOR tables in
// mintopo.cpp.  Pin 0 bridges to the wave filter (the adapter registers the
// physical connection); pin 1 is the endpoint "jack" (speaker or microphone).
enum DISCROD_TOPO_PIN
{
    DiscrodTopoPinWaveBridge = 0,
    DiscrodTopoPinEndpoint   = 1
};

class CMiniportTopology : public IMiniportTopology, public CUnknown
{
public:
    DECLARE_STD_UNKNOWN();
    CMiniportTopology(PUNKNOWN other, DISCROD_ROLE role);
    ~CMiniportTopology();

    // IMP_IMiniportTopology already expands IMP_IMiniport, so this single
    // macro declares Init, GetDescription and DataRangeIntersection.
    IMP_IMiniportTopology;

private:
    DISCROD_ROLE m_role;
};

// Factory used by the adapter when installing the topology subdevice.
NTSTATUS CreateMiniportTopology(_Out_ PUNKNOWN* out, _In_ REFCLSID,
                                _In_opt_ PUNKNOWN outer, _In_ POOL_TYPE pool,
                                _In_ DISCROD_ROLE role);

#endif // _DISCROD_MINTOPO_H_
