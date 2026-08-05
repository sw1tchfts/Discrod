/*++

Module Name:
    adapter.cpp

Abstract:
    Driver entry and adapter for the Discrod Virtual Audio Cable.

    DriverEntry hands off to PortCls.  On StartDevice we:
      1. create the single shared loopback ring,
      2. install the render subdevices  (wave + topology), and
      3. install the capture subdevices (wave + topology),
    then connect each wave filter to its topology so Windows surfaces a
    "Speakers" endpoint and a paired "Microphone" endpoint.  PCM sent to the
    speaker is looped into the microphone via the shared ring.

Environment:
    Kernel mode.

--*/

#include "common.h"
#include "globals.h"
#include "minwavecyclic.h"
#include "mintopo.h"

extern "C" DRIVER_INITIALIZE DriverEntry;
extern "C" DRIVER_ADD_DEVICE AddDevice;

// Forward declarations.
static NTSTATUS StartDevice(PDEVICE_OBJECT, PIRP, PRESOURCELIST);
static NTSTATUS InstallWaveMiniport(PDEVICE_OBJECT, PIRP, DISCROD_ROLE,
                                    PUNKNOWN* outPortWave);
static NTSTATUS InstallTopologyMiniport(PDEVICE_OBJECT, PIRP, DISCROD_ROLE,
                                        PUNKNOWN* outPortTopo);

#pragma code_seg("INIT")
extern "C" NTSTATUS DriverEntry(PDRIVER_OBJECT driverObject,
                                PUNICODE_STRING registryPath)
{
    // PortCls supplies the WDM dispatch routines; we only provide AddDevice and
    // the per-device StartDevice callback (via PcRegisterSubdevice below).
    NTSTATUS status = PcInitializeAdapterDriver(driverObject, registryPath,
                                                AddDevice);
    return status;
}

#pragma code_seg("PAGE")
extern "C" NTSTATUS AddDevice(PDRIVER_OBJECT driverObject,
                              PDEVICE_OBJECT physicalDeviceObject)
{
    PAGED_CODE();

    // Two subdevices max per filter (wave + topology) x two endpoints = 4,
    // plus headroom for PortCls internals.
    return PcAddAdapterDevice(driverObject, physicalDeviceObject,
                              StartDevice, /*maxObjects*/ 8, 0);
}

static NTSTATUS StartDevice(PDEVICE_OBJECT deviceObject, PIRP irp,
                            PRESOURCELIST resourceList)
{
    PAGED_CODE();
    UNREFERENCED_PARAMETER(resourceList);

    NTSTATUS status = InitLoopback();
    if (!NT_SUCCESS(status))
    {
        return status;
    }

    PUNKNOWN renderWavePort = nullptr;
    PUNKNOWN renderTopoPort = nullptr;
    PUNKNOWN captureWavePort = nullptr;
    PUNKNOWN captureTopoPort = nullptr;

    // --- Render endpoint (appears as "Speakers") ---------------------------
    status = InstallWaveMiniport(deviceObject, irp, RoleRender, &renderWavePort);
    if (NT_SUCCESS(status))
    {
        status = InstallTopologyMiniport(deviceObject, irp, RoleRender,
                                         &renderTopoPort);
    }
    if (NT_SUCCESS(status))
    {
        // Wire the wave filter's bridge pin to the topology's bridge pin so
        // the endpoint builder sees one continuous render path.
        status = PcRegisterPhysicalConnection(deviceObject,
                                              renderWavePort,
                                              DiscrodWavePinBridge,
                                              renderTopoPort,
                                              DiscrodTopoPinWaveBridge);
    }

    // --- Capture endpoint (appears as "Microphone") ------------------------
    if (NT_SUCCESS(status))
    {
        status = InstallWaveMiniport(deviceObject, irp, RoleCapture,
                                     &captureWavePort);
    }
    if (NT_SUCCESS(status))
    {
        status = InstallTopologyMiniport(deviceObject, irp, RoleCapture,
                                         &captureTopoPort);
    }
    if (NT_SUCCESS(status))
    {
        // Capture flows the other way: topology bridge (out) -> wave bridge (in).
        status = PcRegisterPhysicalConnection(deviceObject,
                                              captureTopoPort,
                                              DiscrodTopoPinWaveBridge,
                                              captureWavePort,
                                              DiscrodWavePinBridge);
    }

    if (renderWavePort)
    {
        renderWavePort->Release();
    }
    if (renderTopoPort)
    {
        renderTopoPort->Release();
    }
    if (captureWavePort)
    {
        captureWavePort->Release();
    }
    if (captureTopoPort)
    {
        captureTopoPort->Release();
    }

    if (!NT_SUCCESS(status))
    {
        FreeLoopback();
    }
    return status;
}

static NTSTATUS InstallWaveMiniport(PDEVICE_OBJECT deviceObject, PIRP irp,
                                    DISCROD_ROLE role, PUNKNOWN* outPortWave)
{
    PAGED_CODE();

    PPORT port = nullptr;
    NTSTATUS status = PcNewPort(&port, CLSID_PortWaveCyclic);
    if (!NT_SUCCESS(status))
    {
        return status;
    }

    CMiniportWaveCyclic* miniport =
        new (POOL_FLAG_NON_PAGED, DISCROD_POOLTAG)
            CMiniportWaveCyclic(nullptr, role);
    if (miniport == nullptr)
    {
        port->Release();
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    miniport->AddRef();

    status = port->Init(deviceObject, irp,
                        static_cast<PUNKNOWN>(
                            static_cast<IMiniportWaveCyclic*>(miniport)),
                        nullptr, nullptr);
    if (NT_SUCCESS(status))
    {
        PCWSTR ref = (role == RoleRender) ? DISCROD_REF_RENDER_WAVE
                                          : DISCROD_REF_CAPTURE_WAVE;
        status = PcRegisterSubdevice(deviceObject, const_cast<PWSTR>(ref), port);
    }

    miniport->Release();
    if (NT_SUCCESS(status))
    {
        *outPortWave = static_cast<PUNKNOWN>(port);   // caller releases
    }
    else
    {
        port->Release();
    }
    return status;
}

static NTSTATUS InstallTopologyMiniport(PDEVICE_OBJECT deviceObject, PIRP irp,
                                        DISCROD_ROLE role,
                                        PUNKNOWN* outPortTopo)
{
    PAGED_CODE();

    PPORT port = nullptr;
    NTSTATUS status = PcNewPort(&port, CLSID_PortTopology);
    if (!NT_SUCCESS(status))
    {
        return status;
    }

    PUNKNOWN miniport = nullptr;
    status = CreateMiniportTopology(&miniport, GUID_NULL, nullptr,
                                    NonPagedPoolNx, role);
    if (NT_SUCCESS(status))
    {
        status = port->Init(deviceObject, irp, miniport, nullptr, nullptr);
        if (NT_SUCCESS(status))
        {
            PCWSTR ref = (role == RoleRender) ? DISCROD_REF_RENDER_TOPO
                                              : DISCROD_REF_CAPTURE_TOPO;
            status = PcRegisterSubdevice(deviceObject, const_cast<PWSTR>(ref),
                                         port);
        }
        miniport->Release();
    }

    if (NT_SUCCESS(status))
    {
        *outPortTopo = static_cast<PUNKNOWN>(port);   // caller releases
    }
    else
    {
        port->Release();
    }
    return status;
}
