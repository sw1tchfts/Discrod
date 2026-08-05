# Deploying Discrod (self-contained: our own virtual cable driver)

This is the end-to-end path from a clone of this repo to talking in Discord
through the processed virtual microphone, using **our own kernel driver** —
no VB-CABLE or other third-party cable involved.

```
 MIDI keys ─► Discrod app (gate/comp/EQ, soundboard mix)
                   │ writes to
                   ▼
 Discrod Virtual Cable (Speakers)  ── kernel loopback ──►  Discrod Virtual Cable (Microphone)
                                                                   │ selected as input in
                                                                   ▼
                                                                Discord
```

Two machines-worth of roles are involved even if they're the same PC: a **build
machine** (Visual Studio + WDK) and a **test machine** where the driver runs.
For personal use these are usually the same box; just know that test-signing
weakens the kernel's signature policy, so prefer a machine/VM you're
comfortable rebooting and debugging.

> **Honest status:** the driver source is structurally complete and passes a
> mock-portcls link harness, but it has **never been compiled with a real WDK
> or loaded on real Windows**. Expect the first build to surface issues to fix
> on-device — that's the normal driver bring-up loop, and `driver/docs/BUILD.md`
> has the debugging notes.

## 1. Prerequisites (one-time)

On the build machine:

1. **Visual Studio 2022** with the *Desktop development with C++* workload.
2. **Windows SDK** + **Windows Driver Kit (WDK)** matching your VS version,
   including the WDK Visual Studio extension
   (<https://learn.microsoft.com/windows-hardware/drivers/download-the-wdk>).
3. **Python 3.10+** for the app.

On the test machine:

4. Ability to enable **test-signing**. If Secure Boot is enabled in firmware,
   `bcdedit /set testsigning on` is blocked — disable Secure Boot first (you
   can re-enable it after switching to attestation-signed builds).

## Anti-cheat compatibility (Vanguard, EasyAntiCheat, BattlEye)

The dev install below puts the machine into **test-signing mode** and (if
Secure Boot was on) leaves **Secure Boot disabled** — exactly the machine
state kernel anti-cheat refuses to run on. As of Aug 2026:

- **Riot Vanguard** (Valorant, League of Legends) refuses to launch while
  test-signing is enabled, and on Windows 11 additionally requires Secure
  Boot + TPM 2.0 — the well-known **VAN9001 / VAN9003** errors. This is a
  **launch block / integrity refusal, not a cheating ban**: revert the
  machine state and the games launch again.
- **EasyAntiCheat, BattlEye, and FACEIT** are similarly hostile to
  test-signing mode.

None of this software touches game processes. A **properly signed** driver or
APO is ordinary audio software (same category as VB-CABLE or NVIDIA
Broadcast) and is not an anti-cheat concern — the risk is purely the
unsigned-dev machine state the steps below create.

**On a machine you game on**, skip the dev driver install entirely and use an
already-signed transport from [`ALTERNATIVES.md`](ALTERNATIVES.md) (Steam
Streaming Microphone, Elgato Wave Link, VB-CABLE): just select it as
**Virtual mic out** in the app. No test mode, no Secure Boot change, no
registry edits.

**Preflight:** run `scripts\check-anticheat.ps1` (repo root) *before*
installing — it detects Vanguard/EAC/BattlEye/FACEIT and reports the current
test-signing / Secure Boot / protected-audio state — and again *after*
reverting to confirm the machine is back to a state anti-cheat accepts. If
anti-cheat is detected, `install-driver.ps1` (and the APO's
`register-apo.ps1`) refuse to proceed unless you pass
`-AcknowledgeAntiCheatRisk` — with two exceptions: `register-apo.ps1` skips
the gate entirely when `-NoProtectedAudioOverride` is passed (a properly
signed APO makes no risky change to gate), and `install-driver.ps1` only
warns when test-signing is already enabled (that state exists whether or not
the install continues).

**Revert** (then reboot):

```powershell
# Driver path: remove the driver and restore signature enforcement
powershell -ExecutionPolicy Bypass -File driver\scripts\uninstall-driver.ps1 -DisableTestSigning
# ...then re-enable Secure Boot in UEFI firmware settings if you disabled it.

# APO path (if used): detach, restore the endpoint, restore audiodg protection
powershell -ExecutionPolicy Bypass -File apo\scripts\unregister-apo.ps1 -ReenableProtectedAudio
```

Anti-cheat vendors change enforcement over time — the claims above are dated
Aug 2026, so check the vendor's current support pages (e.g. Riot's
VAN9001/VAN9003 articles) rather than trusting this file to age well.

## 2. Build the driver

From a *Developer PowerShell for VS 2022*:

```powershell
cd driver
msbuild DiscrodAudio.vcxproj /p:Configuration=Debug /p:Platform=x64
```

The package (`DiscrodAudio.sys`, `.inf`, `.cat`) lands in
`driver\x64\Debug\DiscrodAudio\`.

## 3. Sign + install (scripted)

From an **elevated** Developer PowerShell on the test machine:

```powershell
cd driver
powershell -ExecutionPolicy Bypass -File scripts\install-driver.ps1 -Build
```

The script does, in order: build (because of `-Build`), create + trust a
self-signed test certificate on first run, sign the `.sys`, regenerate and sign
the catalog, verify test-signing boot mode, stage the package with `pnputil`,
and create the root-enumerated device node (`root\DiscrodAudio`) via
`devgen`/`devcon`.

Two-step first run: if test-signing was off, the script enables it and stops —
**reboot, rerun the same command**, and it proceeds to install.

Prefer manual steps or hit a corner case? Every individual command is in
`driver/docs/BUILD.md`.

### Verify

*Settings → System → Sound* should now list:

- **Discrod Virtual Cable (Speakers)** under Output
- **Discrod Virtual Cable (Microphone)** under Input

Playing any audio to the Speakers side should show level on the Microphone
side's meter. Nothing there? See Troubleshooting below.

## 4. Deploy the app

```powershell
cd app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m discrod
```

In the app's toolbar:

1. **Mic** → your real microphone (pick the *[WASAPI]* entry when offered —
   lowest latency; the app labels each device with its host API).
2. **Virtual mic out** → *Discrod Virtual Cable (Speakers)*.
3. **MIDI** → your keyboard/controller.
4. Bind pads: **MIDI learn** → choose a clip → hit a key. Set each pad's mode
   (one-shot / gate / loop / toggle), target channel, and gain in the table.
5. Open **FX…** on the Microphone strip to enable/tune the gate, compressor,
   and EQ. Suggested starting points for voice: gate threshold −45 dB,
   compressor −18 dB at 3:1, EQ flat.
6. Press **Start**.

## 5. Wire up Discord

*Settings → Voice & Video*:

- **Input Device** → *Discrod Virtual Cable (Microphone)*.
- Turn **off** Discord's noise suppression (Krisp) and automatic gain control —
  the app's gate/compressor already does this, and stacking them pumps.
- Set input mode to *Voice Activity* and calibrate the sensitivity slider while
  speaking through the chain.

Do a test in a private voice channel: talk, fire pads, watch the app's master
meter. If Discord clips, pull the master fader down a few dB.

## 6. Updating

- **App changes:** just `git pull` and restart `python -m discrod`.
- **Driver changes:** rerun `scripts\install-driver.ps1 -Build`; if endpoints
  act stale, run `scripts\uninstall-driver.ps1` first, reboot, reinstall.

## Uninstall

```powershell
cd driver
powershell -ExecutionPolicy Bypass -File scripts\uninstall-driver.ps1
# add -DisableTestSigning to also restore normal signature enforcement
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `bcdedit /set testsigning on` fails with a policy error | Secure Boot is on — disable it in UEFI firmware settings, retry. |
| Device shows **Code 52** in Device Manager | Signature not trusted: confirm test-signing is active (`bcdedit /enum {current}` shows `testsigning Yes` — needs the post-enable reboot) and the test cert is in *Trusted Root* + *Trusted Publishers* (the install script does this). |
| Driver installs but **no endpoints appear** | Interface reference strings must match between `DiscrodAudio.inf` and `driver/src/common.h`; check *Windows → Event Viewer → System* for portcls/DiscrodAudio errors. This is the most likely first-bring-up failure — report the event text and iterate. |
| Endpoints exist but **Discord hears silence** | In the app, confirm output is the cable's *Speakers* side and the engine is running (status bar). Check the master meter moves. In Windows *Sound settings → Microphone (cable)*, confirm the level meter moves while the app runs. |
| **Crackling / dropouts** | Raise the app's block size in `%APPDATA%\Discrod\config.json` (`block_size`: 256 → 512), keep the app on WASAPI devices, and close other exclusive-mode audio apps. |
| **BSOD** on driver load/start | Grab `C:\Windows\Minidump\*.dmp`, open in WinDbg (`!analyze -v`), and file the stack — the driver has never met real hardware, so a bring-up bug is possible. Test in a VM if you want zero risk to your main machine. |
| Pads silent, status bar says a clip can't load | The mapped file moved or is an unsupported format — remap the pad. |
| **Valorant/Vanguard shows VAN9001 or VAN9003** | The machine is still in the unsigned-dev state (test-signing on and/or Secure Boot off). Run `driver\scripts\uninstall-driver.ps1 -DisableTestSigning`, re-enable Secure Boot in UEFI firmware, reboot; if you used the APO path, also run `apo\scripts\unregister-apo.ps1 -ReenableProtectedAudio`. Confirm with `scripts\check-anticheat.ps1`. This is a launch block, not a ban — see *Anti-cheat compatibility* above. |

## Distributing to other people (later)

Test-signing is a per-machine developer mode. To hand this to friends without
firmware fiddling you need an **EV code-signing certificate** and **attestation
signing** through the Microsoft Partner Center (Hardware Dev Center); the
signed package then installs on stock Windows with Secure Boot on. The app side
can be bundled into a single `Discrod.exe` with PyInstaller at that point.
