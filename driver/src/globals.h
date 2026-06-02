/*++

Module Name:
    globals.h

Abstract:
    Process-wide singletons.  The render and capture endpoints are separate
    PortCls subdevices/miniports but must share a single loopback ring so that
    audio written to the speaker side appears on the microphone side.  The
    adapter owns one CLoopbackBuffer and exposes it here.

Environment:
    Kernel mode.

--*/

#ifndef _DISCROD_GLOBALS_H_
#define _DISCROD_GLOBALS_H_

#include "loopbuffer.h"

// Returns the single shared loopback ring (allocated in StartDevice).
CLoopbackBuffer* GetLoopback();

// Lifetime management, called from the adapter.
NTSTATUS InitLoopback();
void     FreeLoopback();

#endif // _DISCROD_GLOBALS_H_
