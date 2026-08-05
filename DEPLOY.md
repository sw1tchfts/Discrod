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
3. **MIDI** → your keyboard/controller.
4. Bind pads: **MIDI learn** → choose a clip → hit a key. Set each pad's mode
   (one-shot / gate / loop / toggle), target channel, and gain in the table.
5. Open **FX…** on the Microphone strip to enable/tune the gate, compressor,
   and EQ. Suggested starting points for voice: gate threshold −45 dB,
   compressor −18 dB at 3:1, EQ flat.
6. Press **Start**.

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
| Pads silent, status bar says a clip can't load | The mapped file moved or is an unsupported format — remap the pad. |
| Worried about **Vanguard / anti-cheat** | Nothing to revert and nothing to check: this setup makes no machine-integrity changes (no test mode, no Secure Boot change, no protected-process overrides). VB-CABLE is a normally-signed driver, the same category as any hardware vendor's. |

## Distributing to other people (later)

The roadmap installer bundles VB-CABLE per its vendor-blessed bundling terms
(attribution + donation notice), silently installs it, and sets the cable as
the default *communications* device so Discord picks it up with zero clicks.
The app side can be bundled into a single `Discrod.exe` with PyInstaller.
