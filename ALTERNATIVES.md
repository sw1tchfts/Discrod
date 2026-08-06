# Routes for getting the processed mic + soundboard into Discord

Deep-dive research (Aug 2026, web-verified) into every way to present our
"processed mic + clips" mix to Discord as an input device, and what each
costs.  Bottom line: **there is no supported way to create a Windows virtual
microphone without a kernel driver** — the only real question is *whose
signature* is on the driver in the path.

> **Decision (Aug 2026): ship on an already-signed cable — VB-CABLE first.**
> Users want plug-and-play, not machine configuration; the signed-cable route
> is the only one that is supported, anti-cheat-safe, and zero-setup today.
> The custom kernel driver (`driver/`) and the experimental injection APO
> (`apo/`) were removed from the tree — both survive in git history if a
> first-party transport is ever wanted again (the realistic revival path is
> the driver + attestation signing, per the analysis below). The app
> auto-detects installed cables — see `cables.py` — and the roadmap installer
> bundles VB-CABLE under its vendor-blessed terms.

## Verified dead ends

- **User-mode virtual audio endpoint API: does not exist.** Windows 11 has a
  user-mode virtual *camera* API (`MFCreateVirtualCamera`, 22H2) but no audio
  analogue in 24H2/25H2, current Insider builds, or the Windows App SDK, and
  none on any public roadmap.  `SwDeviceCreate` can't mint audio endpoints —
  AudioEndpointBuilder only builds them from kernel KS filter interfaces.
- **WASAPI/AudioGraph injection: capture-only.** Process-loopback capture
  reads app audio; nothing injects into a capture path.
- **Discord app-audio input: not a feature.** Desktop Discord lists only
  WASAPI capture endpoints; no API injects into the outgoing voice stream.
- **OBS ecosystem: no virtual microphone.** The virtual camera is video-only;
  every community "virtual mic" recipe bottoms out in a sysvad-style kernel
  driver.

## The injection-APO idea (evaluated, not recommended)

A custom capture APO on the real mic endpoint could apply our DSP *and*
additively mix soundboard audio received over shared memory — the AEC APO
framework proves capture APOs can consume external audio, and Equalizer APO
proves third-party code loads into `audiodg.exe` on current Windows 11.
But: zero prior art of injection specifically; the registration technique is
officially unsupported and wiped by every audio-driver update (needs a
watchdog service); 25H2's per-device "Audio enhancements" dropdown is a
user-flippable kill switch; unsigned APOs require `DisableProtectedAudioDG=1`
(degrades DRM playback system-wide); an APO crash takes down **all** system
audio, with a 10-strike auto-disable.  Total engineering + support burden is
comparable to or worse than the driver.  Every commercial competitor
(Voicemod, Krisp, NVIDIA Broadcast, Elgato Wave Link, SteelSeries Sonar)
looked at this trade and shipped a **signed virtual device driver** instead —
the architecture in `driver/` is the industry-standard one.

### Update: the injection APO was built, then removed with the cable pivot

Because it was the *only* pure-software route meeting a hard "no driver"
requirement, the injection APO was fully implemented (portable C++ core with
sample-for-sample parity against the app's Python DSP; only the Windows COM
shell and registry attach were never proven on real Windows). It was removed
along with `driver/` when the project pivoted to signed cables: the
trade-offs above never improved, and the plug-and-play goal made them moot.
The implementation lives in git history (branch history of PR #3) if ever
worth revisiting.

## Piggybacking an already-signed virtual device

The app can output into *any* render endpoint, so any signed loopback pair on
the machine works as the cable with zero code changes:

| Option | Fit | Notes |
|---|---|---|
| **Steam Streaming Microphone** | Likely drop-in; needs a bench test | Valve's streaming driver exposes "Speakers (Steam Streaming Microphone)" (render) + "Microphone (…)" (capture). Installed on first Remote Play use; community reports it works as a free virtual cable, but loopback-without-a-Steam-session is not authoritatively documented. If Steam is installed: 5-minute zero-install experiment. |
| **Elgato Wave Link 3.0** | Works; adds a second mixer app | Now **free with no Elgato hardware** (since 2.0, Feb 2025). Virtual playback channels mixed into a "Wave Link" mic Discord can select. Their app must run alongside ours. |
| **SteelSeries Sonar** | Works; clunkiest | Free, no hardware, but app audio only reaches the mic mix in Stream Mode via the GG suite. |
| **VB-CABLE** | Cleanest third-party cable | Donationware with an explicit vendor-blessed bundling path (attribution + donation notice). No companion app in the data path. |
| Voicemod / NVIDIA Broadcast | ✗ | Their virtual mics accept no third-party injection (Broadcast also requires RTX hardware). |
| Open-source signed cables | ✗ (none exist) | SAR unsigned/stale; Scream needs test mode on Win11 and is network-oriented; VAC trial watermarks audio; VirtualDrivers/Virtual-Audio-Driver publishes only test-signed betas (sells signed custom builds). |

These signed options are also the **anti-cheat-safe** route: no test-signing
mode, no Secure Boot changes, no `DisableProtectedAudioDG` downgrade — none of
the machine states that make Riot Vanguard refuse to launch (VAN9001/VAN9003)
or that EasyAntiCheat/BattlEye object to (as of Aug 2026 — vendors change
enforcement, so check their current guidance). This is the transport the
product now ships on.

## No-driver approximations

- **Hardware loopback** — an audio interface with a loopback channel
  (Focusrite/MOTU/Rodecaster/GoXLR class), or line-out→line-in with a TRS
  cable, or motherboard "Stereo Mix". Solves both requirements with only the
  vendor's signed drivers; the streamer-standard answer.
- **Discord-native lite mode** — Discord's built-in soundboard (MP3/OGG,
  ≤512 KB, ≤5.2 s, slots by boost level, usable in DMs) supports per-sound
  hotkeys; our app could translate MIDI notes into those keystrokes.  Mic
  processing would ride separately (e.g. Equalizer APO on the real mic).
  Covers short memes; not full-length clips mixed under your voice.
- **Bot account** — sanctioned by Discord: a bot in a guild voice channel can
  fire `POST /channels/{id}/send-soundboard-sound` or stream Opus on
  MIDI-triggered RPC.  Clips arrive under the bot's identity, guild channels
  only (no DM calls).  Self-botting a user account is ToS-forbidden.

## Recommendation

Adopted (see the decision note at the top): ship on signed cables, VB-CABLE
first, with the app auto-detecting whatever signed loopback is already
installed. If a first-party transport is ever justified (branding, support
control), the path is reviving `driver/` from git history and paying for EV +
attestation signing — not the APO.
