# Deploying Discrod (signed virtual cable: VB-CABLE)

The end-to-end path from a clone of this repo to talking in Discord through
the processed virtual microphone. The transport is an **already-signed
virtual audio cable** — no custom drivers, no test-signing, no system
modifications, and therefore nothing that bothers kernel anti-cheat
(Vanguard, EasyAntiCheat, BattlEye: as of Aug 2026 those object to
*unsigned-dev machine state*, not to ordinary signed audio devices).

```
 MIDI keys ─► Discrod app (gate/comp/EQ, soundboard mix)
                   │ writes to
                   ▼
 CABLE Input (render)  ── signed loopback ──►  CABLE Output (capture)
                                                    │ selected as input in
                                                    ▼
                                                 Discord
```

## 1. Install VB-CABLE (one time)

1. Download the driver pack from <https://vb-audio.com/Cable/>.
2. Extract, right-click `VBCABLE_Setup_x64.exe` → **Run as administrator**.
3. Reboot if the installer asks.

VB-CABLE is donationware — if it serves you well,
[support the author](https://vb-audio.com/Cable/).

After install, *Settings → System → Sound* lists **CABLE Input** under Output
and **CABLE Output** under Input.

> **Already have a signed cable?** The app also auto-detects the **Steam
> Streaming Microphone** (installed by Steam Remote Play), **Elgato Wave
> Link**, and **VoiceMeeter** — any of them works with zero extra installs.
> See [`ALTERNATIVES.md`](ALTERNATIVES.md).

## 2. Run the app

Double-click **`start-discrod.bat`** in the repo root — it creates the
virtual environment and installs dependencies on first run (and re-installs
them automatically whenever `requirements.txt` changes), then launches the
app. Requires [Python 3.10+](https://www.python.org/downloads/) installed
with *Add python.exe to PATH* ticked.

Manual equivalent:

```powershell
cd app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m discrod
```

In the app's toolbar:

1. **Virtual mic out** — auto-selected on first run when a known cable is
   found (the status bar names it). To change it, just pick another device;
   your choice is saved.
2. **Mic** → your real microphone (pick the *[WASAPI]* entry when offered —
   lowest latency; the app labels each device with its host API).
3. **Monitor** (optional) → your headphones, to hear the soundboard at the
   exact level Discord receives it. Tick **Hear mic FX** to also audition
   your processed mic — headphones only, speakers will feed back into the
   mic. The toggle works live while the engine runs.
4. **MIDI** → your keyboard/controller.
5. Bind pads: **MIDI learn** → choose a clip → hit a key. Set each pad's mode
   (one-shot / gate / loop / toggle), target channel, and gain in the table.
6. Open **FX…** on the Microphone strip to enable/tune the gate, compressor,
   and EQ. Suggested starting points for voice: gate threshold −45 dB,
   compressor −18 dB at 3:1, EQ flat.
7. Press **Start**.

## 3. Wire up Discord

*Settings → Voice & Video*:

- **Input Device** → *CABLE Output (VB-Audio Virtual Cable)*.
- Turn **off** Discord's noise suppression (Krisp) and automatic gain control —
  the app's gate/compressor already does this, and stacking them pumps.
- Set input mode to *Voice Activity* and calibrate the sensitivity slider while
  speaking through the chain.

Do a test in a private voice channel: talk, fire pads, watch the app's master
meter. If Discord clips, pull the master fader down a few dB.

## 4. Updating

App changes: `git pull` and restart `python -m discrod`. The cable never
needs touching once installed.

## Uninstall

Remove VB-CABLE with its own `VBCABLE_Setup_x64.exe` (Remove Driver button,
as administrator), then reboot. The app itself is just the `app/` folder and
`%APPDATA%\Discrod\` (config).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No CABLE devices in Windows sound settings | The installer needs *Run as administrator* and a reboot — redo both. |
| App didn't auto-select the cable | Press the **⟳** rescan button, or pick *CABLE Input* manually under **Virtual mic out**. |
| Discord doesn't list CABLE Output | Restart Discord after installing the cable (it snapshots devices at launch). |
| Discord hears **silence** | Confirm the engine is running (status bar) and the master meter moves; in Windows *Sound settings → CABLE Output*, confirm its level meter moves while the app runs. |
| **Crackling / dropouts** | Raise the app's block size in `%APPDATA%\Discrod\config.json` (`block_size`: 256 → 512), keep the app on WASAPI devices, and close other exclusive-mode audio apps. |
| Clicking on the **Monitor** output | The monitor rides ~64 ms behind the live path and a rate servo absorbs up to ±5% clock slew between the cable and your headphones, declicking anything that still slips through. If it persists: **align sample rates to 48000 Hz everywhere** — VB-CABLE's own control panel (its internal rate defaults to 44100 on some installs), plus *Sound Control Panel → Properties → Advanced* for CABLE Input, CABLE Output, and your headphones. The status bar shows the negotiated rate and live glitch counters (`… Hz · glitches mic N / monitor M`) — if the mic counter climbs, the problem is the live path, not the monitor; report which counter moves. Prefer WASAPI entries for all three devices, and raise `block_size` as above. |
| Pads silent, status bar says a clip can't load | The mapped file moved or is an unsupported format — remap the pad. |
| `Invalid sample rate [PaErrorCode -9997]` on Start | Your devices disagree on a common rate. The app now negotiates across every selected device (plus 48000/44100 fallbacks) and, when no shared rate exists, opens the mic/monitor at their own rates and converts internally — so update to the latest build first. The status bar shows per-stream rates when they differ (e.g. `48000 Hz (mon 44100)`). Aligning everything to 48000 Hz in Windows' Advanced sound properties is still the cleanest setup. |
| **Steam Streaming Microphone** glitches / has no WASAPI entry | Expected: Valve's device is an old-style driver (MME/DirectSound only) with a bursty software clock — the servo absorbs a lot of it, but it remains the flakiest cable option. If the glitch counters keep climbing on it, install VB-CABLE; the app auto-prefers VB-CABLE the moment it exists. |
| Worried about **Vanguard / anti-cheat** | Nothing to revert and nothing to check: this setup makes no machine-integrity changes (no test mode, no Secure Boot change, no protected-process overrides). VB-CABLE is a normally-signed driver, the same category as any hardware vendor's. |

## Distributing to other people (later)

The roadmap installer bundles VB-CABLE per its vendor-blessed bundling terms
(attribution + donation notice), silently installs it, and sets the cable as
the default *communications* device so Discord picks it up with zero clicks.
The app side can be bundled into a single `Discrod.exe` with PyInstaller.
