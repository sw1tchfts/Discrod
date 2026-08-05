/*++

Module Name:
    globals.cpp

Abstract:
    Definition of the shared loopback ring singleton.  See globals.h.

Environment:
    Kernel mode.

--*/

#include "globals.h"

#pragma code_seg("PAGE")

static CLoopbackBuffer* g_loopback = nullptr;

NTSTATUS InitLoopback()
{
    PAGED_CODE();

    if (g_loopback != nullptr)
    {
        return STATUS_SUCCESS;
    }

    g_loopback = new (POOL_FLAG_NON_PAGED, DISCROD_POOLTAG) CLoopbackBuffer();
    if (g_loopback == nullptr)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    NTSTATUS status = g_loopback->Init(DISCROD_RING_BYTES);
    if (!NT_SUCCESS(status))
    {
        delete g_loopback;
        g_loopback = nullptr;
    }
    return status;
}

void FreeLoopback()
{
    PAGED_CODE();
    if (g_loopback != nullptr)
    {
        delete g_loopback;
        g_loopback = nullptr;
    }
}

#pragma code_seg()  // GetLoopback is called from the timer DPC at DISPATCH_LEVEL

CLoopbackBuffer* GetLoopback()
{
    return g_loopback;
}
