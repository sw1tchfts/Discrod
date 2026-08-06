"""Tests for signed virtual-cable detection (audio/cables.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discrod.audio.cables import (  # noqa: E402
    KNOWN_CABLES,
    find_virtual_cables,
    pick_virtual_cable,
)


# A realistic Windows PortAudio listing: same device once per host API, with
# the MME entries truncated to 31 characters as PortAudio really does.
WINDOWS_OUTPUTS = [
    (3, "Speakers (Realtek(R) Audio)", "MME"),
    (4, "CABLE Input (VB-Audio Virtual", "MME"),           # MME-truncated
    (9, "Speakers (Realtek(R) Audio)", "Windows DirectSound"),
    (10, "CABLE Input (VB-Audio Virtual Cable)", "Windows DirectSound"),
    (14, "Speakers (Realtek(R) Audio)", "Windows WASAPI"),
    (15, "CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI"),
]


def test_vb_cable_found_and_prefers_wasapi():
    match = pick_virtual_cable(WINDOWS_OUTPUTS)
    assert match is not None
    assert match.product.key == "vb-cable"
    assert match.device[0] == 15  # the WASAPI entry, not MME/DirectSound


def test_mme_truncated_name_still_matches():
    outputs = [(4, "CABLE Input (VB-Audio Virtual", "MME")]
    match = pick_virtual_cable(outputs)
    assert match is not None
    assert match.product.key == "vb-cable"
    assert match.device[0] == 4  # no WASAPI duplicate -> first listed


def test_priority_vb_cable_over_steam():
    outputs = [
        (2, "Speakers (Steam Streaming Microphone)", "Windows WASAPI"),
        (5, "CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI"),
    ]
    match = pick_virtual_cable(outputs)
    assert match.product.key == "vb-cable"


def test_steam_when_no_vb_cable():
    outputs = [
        (1, "Speakers (Realtek(R) Audio)", "Windows WASAPI"),
        (2, "Speakers (Steam Streaming Microphone)", "Windows WASAPI"),
    ]
    match = pick_virtual_cable(outputs)
    assert match.product.key == "steam"
    assert match.device[0] == 2


def test_case_insensitive():
    outputs = [(0, "cable input (vb-audio virtual cable)", "ASIO")]
    assert pick_virtual_cable(outputs) is not None


def test_no_cable_returns_none():
    outputs = [
        (0, "Speakers (Realtek(R) Audio)", "Windows WASAPI"),
        (1, "Headphones (USB DAC)", "Windows WASAPI"),
    ]
    assert pick_virtual_cable(outputs) is None
    assert find_virtual_cables(outputs) == []


def test_find_returns_priority_order_then_listing_order():
    outputs = [
        (7, "Wave Link Stream (Elgato Virtual Audio)", "Windows WASAPI"),
        (8, "CABLE Input (VB-Audio Virtual Cable)", "MME"),
        (9, "CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI"),
    ]
    found = find_virtual_cables(outputs)
    assert [m.product.key for m in found] == ["vb-cable", "vb-cable", "wave-link"]
    assert [m.device[0] for m in found] == [8, 9, 7]


def test_empty_listing():
    # Headless machines / PortAudio unavailable: list_devices() returns [].
    assert pick_virtual_cable([]) is None


def test_known_cables_render_substrings_are_lowercase():
    # _matches() lowercases only the device name; the patterns must already
    # be lowercase or they can never match.
    for product in KNOWN_CABLES:
        assert product.render_substr == product.render_substr.lower()
