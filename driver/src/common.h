/*++

Module Name:
    common.h

Abstract:
    Shared definitions, GUIDs and pool tags for the Discrod Virtual Audio Cable
    driver.  The driver registers a paired render (speaker) and capture
    (microphone) endpoint; PCM frames written to the render endpoint are looped
    back to the capture endpoint through a shared ring buffer, which is what
    makes it usable as a "virtual microphone" in apps such as Discord.

Environment:
    Kernel mode.

--*/

#ifndef _DISCROD_COMMON_H_
#define _DISCROD_COMMON_H_

#include <portcls.h>
#include <stdunk.h>
#include <ksdebug.h>

// Pool tag: 'DscA' (Discrod Audio), shown in !poolused for leak hunting.
#define DISCROD_POOLTAG 'AcsD'

// Which endpoint a miniport instance serves.
enum DISCROD_ROLE { RoleRender, RoleCapture };

// Friendly names (must match the INF strings).
#define DISCROD_RENDER_NAME   L"Discrod Virtual Cable (Speakers)"
#define DISCROD_CAPTURE_NAME  L"Discrod Virtual Cable (Microphone)"

// Engine format the virtual device advertises.  48 kHz / 16-bit / stereo is the
// safe lowest-common-denominator that Discord and Windows mixers accept.
#define DISCROD_SAMPLE_RATE   48000
#define DISCROD_CHANNELS      2
#define DISCROD_BITS_PER_SAMPLE 16
#define DISCROD_BLOCK_ALIGN   (DISCROD_CHANNELS * DISCROD_BITS_PER_SAMPLE / 8)
#define DISCROD_AVG_BYTES_PER_SEC (DISCROD_SAMPLE_RATE * DISCROD_BLOCK_ALIGN)

// Size of the shared loopback ring, in milliseconds of audio.  Large enough to
// absorb scheduling jitter between the render writer and capture reader.
#define DISCROD_RING_MS       200
#define DISCROD_RING_BYTES \
    ((DISCROD_AVG_BYTES_PER_SEC / 1000) * DISCROD_RING_MS)

#if (NTDDI_VERSION >= NTDDI_WIN10)
#define DISCROD_WAVERT 1
#endif

// Subdevice reference strings used when registering with PortCls.
#define DISCROD_REF_RENDER_WAVE   L"DiscrodRenderWave"
#define DISCROD_REF_RENDER_TOPO   L"DiscrodRenderTopo"
#define DISCROD_REF_CAPTURE_WAVE  L"DiscrodCaptureWave"
#define DISCROD_REF_CAPTURE_TOPO  L"DiscrodCaptureTopo"

#endif // _DISCROD_COMMON_H_
