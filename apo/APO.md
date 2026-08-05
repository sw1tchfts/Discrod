# Discrod capture APO — the driver-free transport

This directory is an alternative to the kernel `driver/`: instead of installing
a virtual audio cable, the **capture APO** runs your mic DSP chain and the
soundboard mix *inside* Windows' audio engine, on your **real microphone**.
Discord then selects that same microphone and receives processed mic +
soundboard — **with no audio device installed** and **nothing to kernel-sign**.

```
  Discrod app  ──(shared memory: soundboard audio + DSP params)──▶  DiscrodApo.dll
  (user session)                                                    (in audiodg.exe)
                                                                          │
                    real microphone  ─────────────────────────────▶  APOProcess:
                                                                    gate → comp → EQ
                                                                    + mix soundboard
                                                                          │
                                                                          ▼
                                                                     Discord (picks
                                                                     the real mic)
```

## Honest status

- The **signal core** (`src/dsp.cpp`, `src/apo_core.cpp`) is portable C++,
  builds on Linux and Windows, and is covered by `tests/apo_tests.cpp` plus a
  cross-language parity check against the Python DSP
  (`app/tests/test_apo_bridge.py` drives `dsp_oracle`). This is the same code
  that would run inside `audiodg.exe`, and it is exercised on every push.
- The **COM/APO layer** (`src/discrod_apo.cpp`, `src/dllmain.cpp`) is written
  against the documented `IAudioProcessingObject*` interfaces and the sysvad
  SWAP sample, but **has never been compiled or loaded on Windows** — it needs
  the Windows SDK headers (`audioenginebaseapo.h`) that only exist there.
- **Attaching an APO to an endpoint whose INF you do not own is not officially
  supported.** `scripts/register-apo.ps1` uses the documented
  `PKEY_FX_*EffectClsid` properties and always backs up what it overwrites, but
  the exact keys the in-box APO proxy honors have shifted across Windows builds.
  Treat this as an experimental path; the signed `driver/` remains the
  fully-supported route.

If the APO path does not load on your build of Windows, nothing is lost: restore
with `unregister-apo.ps1` and use the driver or one of the already-signed
options in `../ALTERNATIVES.md`.

## Build

Portable core + tests (any platform with a C++20 compiler):

```bash
cd apo
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/apo_tests            # core + ring/seqlock tests
```

The full DLL builds only on Windows with the SDK/WDK installed:

```powershell
cd apo
cmake -S . -B build -A x64      # picks up the Windows SDK
cmake --build build --config Release
# -> build\Release\DiscrodApo.dll
```

The parity/round-trip tests in `app/tests/test_apo_bridge.py` auto-detect the
`dsp_oracle` and `bridge_probe` binaries under `apo/build/` and skip if they are
not built, so `pytest` passes in a Python-only checkout.

## Register + attach (Windows, elevated PowerShell)

```powershell
# 1. Register the COM object, allow audiodg to load it, attach to your mic.
apo\scripts\register-apo.ps1            # prompts you to pick the microphone

# 2. (recommended) keep it attached across audio-engine resets:
apo\scripts\watchdog-apo.ps1 -Install   # logon scheduled task

# To remove everything and restore the endpoint:
apo\scripts\unregister-apo.ps1 -ReenableProtectedAudio
```

`register-apo.ps1` sets `DisableProtectedAudioDG` so a self-built / test-signed
DLL can load — this lowers `audiodg` protection **system-wide** and is meant for
a dev/test machine. On a machine you care about, sign `DiscrodApo.dll` with a
trusted code-signing certificate and pass `-NoProtectedAudioOverride`.

> **Anti-cheat note (as of Aug 2026):** how kernel anti-cheat reacts to
> `DisableProtectedAudioDG=1` is not publicly documented — treat it as
> "avoid on a machine running Riot Vanguard, EasyAntiCheat, or BattlEye"
> rather than a confirmed block. `register-apo.ps1` therefore refuses to run
> when those are detected unless you pass `-AcknowledgeAntiCheatRisk`. A
> properly signed APO installed with `-NoProtectedAudioOverride` never touches
> the value and is ordinary audio software, not an anti-cheat concern. Check
> your state with the repo-root `scripts\check-anticheat.ps1`, see the
> *Anti-cheat compatibility* section in [`../DEPLOY.md`](../DEPLOY.md), and
> consult your anti-cheat vendor's current guidance — enforcement changes over
> time.

## Use

1. Start the Discrod app, tick **APO (no driver)**, then **Start**. The app maps
   `%ProgramData%\Discrod\apo_bridge.bin`, publishes the mic-channel parameters,
   and streams the soundboard bus into the shared ring. No audio device is
   opened.
2. In **Discord → Voice & Video → Input Device**, select the **real microphone**
   you attached the APO to.
3. Speak / fire pads. The mic passes through gate → compressor → EQ and the
   soundboard is mixed on top, all inside the audio engine.

## How the bridge works

`src/bridge_protocol.h` is the single source of truth for the shared-memory
layout; `app/discrod/audio/apo_bridge.py` mirrors it byte-for-byte (a drift
fails `test_apo_bridge.py`). Key properties:

- **One writer per field.** The app owns the ring format, `write_pos`, and the
  parameter block; the APO owns `read_pos` and the `apo_*` feedback fields.
- **SPSC ring** with free-running `u32` frame counters (wrap-safe), primed like
  the engine's mic ring so app-scheduler jitter never underruns the RT thread,
  and lap-protected so a stalled reader can never read torn frames.
- **Seqlock params.** The app bumps `param_seq` odd, writes the block, bumps it
  even; the RT reader takes a consistent snapshot or keeps last block's params.
- **No resampler in the RT path.** The APO publishes its stream rate; the app
  renders to match. If the rates disagree the soundboard is silent (mic still
  passes) rather than pitch-shifted.
- **Passthrough by default.** With no bridge, before the app publishes params,
  or on a validation failure, the APO is a bit-exact mic passthrough.
