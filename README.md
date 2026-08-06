# Discrod

A Windows desktop app that turns a MIDI controller into a **soundboard** and
mixes your **microphone + triggered audio clips** into a **virtual microphone**
you can select in Discord — so you can fire off clips *and* keep talking. Every
audio source is its own mixer channel with independent processing (gain, mute,
solo; gate, compressor, and parametric EQ built in, with more DSP on the
roadmap).

The virtual microphone is an **already-signed virtual audio cable** —
[VB-CABLE](https://vb-audio.com/Cable/) is the recommended one — which the app
**auto-detects and selects on first run**. No custom drivers, no test mode, no
system modifications: everything in the audio path is ordinary signed software,
which also means it is **kernel anti-cheat safe** (Vanguard, EAC, BattlEye —
nothing here changes machine integrity state).

```
 MIDI keys ─► clip player ─┐
                           ├─► per-channel DSP ─► mix ─► CABLE Input ─► CABLE Output ─► Discord mic
 Microphone ───────────────┘                             (signed virtual cable, e.g. VB-CABLE)
```

## Quick start

1. **Install VB-CABLE** (one time): download from
   [vb-audio.com/Cable](https://vb-audio.com/Cable/), run
   `VBCABLE_Setup_x64.exe` **as administrator**, reboot if asked. It's
   donationware — [support the author](https://vb-audio.com/Cable/) if it
   serves you well. *(Already have Steam? The Steam Streaming Microphone
   device also works and the app detects it too — see
   [`ALTERNATIVES.md`](ALTERNATIVES.md).)*
2. **Run the app:** double-click **`start-discrod.bat`** (repo root). First
   run sets up the environment and installs dependencies automatically; it
   only needs [Python 3.10+](https://www.python.org/downloads/) installed
   with *Add python.exe to PATH* ticked. (Manual equivalent:

   ```bash
   cd app
   python -m venv .venv && .venv\Scripts\activate      # Windows
   pip install -r requirements.txt
   python -m discrod
   ```

   )

3. In the app: the **Virtual mic out** is auto-selected if a known cable is
   installed (status bar confirms it). Pick your **Mic** and **MIDI**
   controller, add pads (or use **MIDI learn**), press **Start**.
4. In **Discord**: set the input device to **CABLE Output (VB-Audio Virtual
   Cable)**.

Full setup + troubleshooting: [`DEPLOY.md`](DEPLOY.md).

## Features

- **MIDI-triggered soundboard** with one-shot (re-trigger restarts), gate
  (hold-to-play), loop (press again to stop) and toggle modes; polyphonic
  playback; MIDI-learn binding; clips are pre-decoded off the GUI thread and
  every stop/re-trigger is declicked with a short fade.
- **Per-channel mixer** — independent gain, mute, solo and a full processing
  chain on every source (mic, soundboard, and any channels you add).
- **DSP, already implemented:** noise **gate**, **compressor**, and a 3-band
  parametric **EQ** (RBJ biquads), plus master bus with peak meters.  The whole
  chain is vectorized (scipy/numpy) and renders a full-FX block in well under
  1 ms — comfortably inside the 5.3 ms real-time budget.
- **Virtual microphone via signed cables, auto-detected** — VB-CABLE first,
  and the Steam Streaming Microphone, Elgato Wave Link, or VoiceMeeter if
  they're already installed. No custom kernel code anywhere in the product.
- **Device-robust audio I/O** — mono mics are upmixed automatically, sample
  rates are negotiated with the selected devices, and the mic ring buffer is
  primed and drift-bounded so long sessions neither crackle nor accumulate
  latency.
- **Persistent config** — devices (stored by name, so replugging USB gear
  doesn't silently swap them), channels, processing and pad mappings are saved
  between sessions.

## Repository layout

| Path | What it is |
|------|------------|
| `app/` | The desktop application (Python + PySide6). MIDI input, clip playback, the mixer, DSP, and signed-cable auto-detection. |

> Earlier iterations shipped a custom WDK kernel driver and an experimental
> capture APO. Both were removed in favor of the signed-cable transport —
> simpler, supported, and anti-cheat safe. The rationale (and every transport
> option evaluated) lives in [`ALTERNATIVES.md`](ALTERNATIVES.md); the code
> remains in git history if ever needed.

## Roadmap

- **One-click installer** bundling VB-CABLE (per its vendor-blessed bundling
  terms: attribution + donation notice) and pre-wiring the default
  communications device, so Discord picks the mic up automatically.
- More DSP: multiband EQ, limiter/de-esser, sidechain, convolution reverb.
- Per-pad waveform preview, drag-and-drop clip assignment, pad grid view.

## Testing

```bash
cd app && python -m pytest -q
```

The mixing/DSP core and cable detection are unit-tested hardware-free; audio
I/O, MIDI, and the Qt UI need real devices/display to exercise.
