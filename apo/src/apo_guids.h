// COM class + property identifiers for the Discrod capture APO.
//
// The CLSID identifies the APO COM object; the app's registration tool writes
// it into the capture endpoint's FX property store so the audio engine loads
// this APO into the mode-effects (MFX) chain for the real microphone.
//
// This GUID is stable and must never change once anything has been registered
// against it.  Regenerate ONLY if you fork into a genuinely separate product.

#pragma once

#include <initguid.h>
#include <guiddef.h>

// {7C9D5A24-6B3E-4C1F-9A2D-3E8F1B0C6A50}
DEFINE_GUID(CLSID_DiscrodCaptureApo,
    0x7c9d5a24, 0x6b3e, 0x4c1f, 0x9a, 0x2d, 0x3e, 0x8f, 0x1b, 0x0c, 0x6a, 0x50);
