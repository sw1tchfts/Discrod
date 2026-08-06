"""Detection of already-installed signed virtual-cable devices.

Discrod's transport is a signed loopback pair: the app plays the processed
mix into the pair's *render* endpoint and Discord captures the paired
*capture* endpoint.  This module recognizes known products in a PortAudio
device listing so the UI can auto-select one on first run instead of making
the user study device names.

VB-CABLE is the documented/bundled product and matches first; the rest are
free wins many users already have installed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CableProduct:
    key: str
    product: str        # human-readable product name
    render_substr: str  # lowercase substring of the render (output) device name
    discord_mic: str    # what the user selects as Discord's input device


# Priority order: first match wins in pick_virtual_cable().
KNOWN_CABLES = (
    CableProduct(
        key="vb-cable",
        product="VB-CABLE",
        render_substr="cable input",
        discord_mic="CABLE Output (VB-Audio Virtual Cable)",
    ),
    CableProduct(
        key="steam",
        product="Steam Streaming Microphone",
        render_substr="steam streaming",
        discord_mic="Microphone (Steam Streaming Microphone)",
    ),
    CableProduct(
        key="wave-link",
        product="Elgato Wave Link",
        render_substr="wave link",
        discord_mic="the Wave Link microphone",
    ),
    CableProduct(
        key="voicemeeter",
        product="VoiceMeeter",
        render_substr="voicemeeter input",
        discord_mic="VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)",
    ),
)


@dataclass(frozen=True)
class CableMatch:
    device: tuple           # (index, name, hostapi) as from list_devices()
    product: CableProduct


def _matches(product: CableProduct, name: str) -> bool:
    # Substring, case-insensitive: PortAudio's MME backend truncates device
    # names to 31 characters, so full-name equality would miss real devices.
    return product.render_substr in name.lower()


def find_virtual_cables(outputs) -> list[CableMatch]:
    """All recognized cable render endpoints, in KNOWN_CABLES priority order.

    ``outputs`` is the ``[(index, name, hostapi), ...]`` list from
    ``list_devices()``.  Windows lists one entry per host API for the same
    device; every matching entry is returned (callers that open a stream
    should prefer the WASAPI entry — see pick_virtual_cable).
    """
    found = []
    for product in KNOWN_CABLES:
        for dev in outputs:
            if _matches(product, dev[1]):
                found.append(CableMatch(device=tuple(dev), product=product))
    return found


def pick_virtual_cable(outputs) -> CableMatch | None:
    """Best single choice for first-run auto-selection, or None.

    Highest-priority product wins; among that product's host-API duplicates
    the WASAPI entry is preferred (lowest-latency shared-mode path), falling
    back to the first listed.
    """
    matches = find_virtual_cables(outputs)
    if not matches:
        return None
    best_product = matches[0].product
    same = [m for m in matches if m.product is best_product]
    for m in same:
        if "wasapi" in (m.device[2] or "").lower():
            return m
    return same[0]
