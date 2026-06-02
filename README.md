# Discrod

A Windows desktop app that turns a MIDI controller into a **soundboard** and
mixes your **microphone + triggered audio clips** into a **virtual microphone**
you can select in Discord — so you can fire off clips *and* keep talking. Every
audio source is its own mixer channel with independent processing (gain, mute,
solo today; gate, compressor, and parametric EQ already built in, with more
DSP on the roadmap).

It ships with **our own virtual audio cable driver** — no third-party VB-CABLE
required.

```
 MIDI keys ─► clip player ─┐
                           ├─► per-channel DSP ─► mix ─► Discrod Virtual Cable ─► Discord mic
 Microphone ───────────────┘                              (our own driver)
```

## Repository layout

| Path | What it is |
|------|------------|
| `app/` | The desktop application (Python + PySide6). MIDI input, clip playback, the mixer, and DSP. Cross-platform code; targets Windows for the virtual mic. |
| `driver/` | **Our own** virtual audio cable: a Windows WDK PortCls/WaveCyclic kernel driver that exposes paired speaker + microphone endpoints linked by a loopback ring. |

## How the pieces fit

1. The **driver** registers two endpoints: *Discrod Virtual Cable (Speakers)* and
   *Discrod Virtual Cable (Microphone)*. Audio written to the speaker side is
   looped back to the mic side.
2. The **app** captures your real mic, mixes in MIDI-triggered clips through
   per-channel processing, and sends the result to the cable's **speaker**
   endpoint.
3. **Discord** selects the cable's **microphone** endpoint as its input and
   receives your processed mic + clips.

## Quick start (app)

> The driver must be built and installed separately on Windows — see
> [`driver/docs/BUILD.md`](driver/docs/BUILD.md). The app runs without it, but
> the virtual-mic routing needs the driver (or any output device) selected.

```bash
cd app
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
python -m discrod
```

In the app:

1. **Mic** → your real microphone.
2. **Virtual mic out** → *Discrod Virtual Cable (Speakers)*.
3. **MIDI** → your controller.
4. Add pads (or use **MIDI learn**) to bind notes to audio clips, choose the
   target channel and play mode (one-shot / gate / loop / toggle).
5. Press **Start**. In Discord, set the input device to
   *Discrod Virtual Cable (Microphone)*.

## Features

- **MIDI-triggered soundboard** with one-shot, gate (hold-to-play), loop and
  toggle modes; polyphonic playback; MIDI-learn binding.
- **Per-channel mixer** — independent gain, mute, solo and a full processing
  chain on every source (mic, soundboard, and any channels you add).
- **DSP, already implemented:** noise **gate**, **compressor**, and a 3-band
  parametric **EQ** (RBJ biquads), plus master bus with peak meters.
- **Virtual microphone** via our own kernel driver — no VB-CABLE.
- **Persistent config** — devices, channels, processing and pad mappings are
  saved between sessions.

## Roadmap

- More DSP: multiband EQ, limiter/de-esser, sidechain, convolution reverb.
- WaveRT migration in the driver for lower latency.
- Per-pad waveform preview, drag-and-drop clip assignment, pad grid view.
- Driver volume node wired to the topology.

## Testing

```bash
# App (hardware-free mixing + DSP tests)
cd app && python -m pytest -q

# Driver loopback ring algorithm
cd driver/tests && g++ -std=c++17 test_loopbuffer.cpp -o t && ./t
```

## Status & honesty notes

- The **app** is runnable and its mixing/DSP core is unit-tested. Audio I/O,
  MIDI, and the Qt UI need real devices/display to exercise.
- The **driver** is a complete, structured WDK project with the loopback core
  fully implemented and algorithmically verified. It must be compiled,
  test-signed, and iterated on a Windows machine — see
  [`driver/docs/ARCHITECTURE.md`](driver/docs/ARCHITECTURE.md) for what is
  intentionally minimal in v1.
