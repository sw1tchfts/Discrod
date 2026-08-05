# Building & installing the Discrod Virtual Audio Cable driver

> **Scripted path:** `scripts\install-driver.ps1 -Build` automates everything
> below (cert creation, signing, catalog, test-signing check, pnputil install,
> device-node creation), and `scripts\uninstall-driver.ps1` reverses it. The
> repo-root [`DEPLOY.md`](../../DEPLOY.md) is the end-to-end deployment guide;
> this file remains the reference for the individual commands.

> **Important:** this is a Windows **kernel-mode** driver. It can only be built
> on Windows with the WDK, and it cannot be built or tested in the Linux CI
> container used for the app. The loopback ring algorithm (the core of the
> cable) is verified independently by `driver/tests/test_loopbuffer.cpp`, but the
> PortCls miniport must be compiled, signed, and iterated on a real Windows
> machine. Treat this as a v1 driver you finish on-device.

## Prerequisites

1. **Visual Studio 2022** with *Desktop development with C++*.
2. The **Windows Driver Kit (WDK)** matching your Visual Studio version, plus the
   corresponding **Windows SDK**. Install the *WDK Visual Studio extension* so
   the "Driver" project type and `stampinf`/`inf2cat` targets are available.
3. A **test machine** (or VM) where you can enable test-signing. Never test
   unsigned kernel drivers on your main install.

## Build

From a *Developer Command Prompt for VS 2022*:

```bat
cd driver
msbuild DiscrodAudio.vcxproj /p:Configuration=Debug /p:Platform=x64
```

Output lands in `driver\x64\Debug\DiscrodAudio\` (the `.sys`, `.inf`, and a
generated `.cat`).

## Test-signing (development)

Production audio drivers need an EV code-signing certificate and a WHQL/Attestation
submission to the Microsoft Hardware Dev Center. For development, use a
self-signed test certificate and put the machine in test-signing mode:

```bat
:: one-time: create a test certificate
makecert -r -pe -ss PrivateCertStore -n CN=DiscrodTestCert DiscrodTest.cer

:: sign the driver and catalog
signtool sign /v /s PrivateCertStore /n DiscrodTestCert /fd sha256 ^
    /t http://timestamp.digicert.com x64\Debug\DiscrodAudio\DiscrodAudio.sys
inf2cat /driver:x64\Debug\DiscrodAudio /os:10_X64
signtool sign /v /s PrivateCertStore /n DiscrodTestCert /fd sha256 ^
    x64\Debug\DiscrodAudio\DiscrodAudio.cat

:: enable test signing on the TEST machine, then reboot
bcdedit /set testsigning on
```

Install `DiscrodTest.cer` into *Trusted Root Certification Authorities* and
*Trusted Publishers* on the test machine.

## Install

```bat
:: from an elevated prompt on the test machine
pnputil /add-driver DiscrodAudio.inf /install
```

Because this is a root-enumerated software device, also create the device node
once (e.g. with `devgen` from the WDK, or the `swdevice` API in an installer):

```bat
devgen /add /instanceid 0 /hardwareid root\DiscrodAudio
```

After install you should see **Discrod Virtual Cable (Speakers)** and
**Discrod Virtual Cable (Microphone)** in *Sound settings*.

## Using it with Discord

1. In the Discrod app, set **Virtual mic out** to *Discrod Virtual Cable (Speakers)*.
2. In Discord → *Voice & Video → Input Device*, choose
   *Discrod Virtual Cable (Microphone)*.
3. Start the engine in the app. Your mic + triggered clips are mixed and appear
   on Discord's input.

## Uninstall

```bat
pnputil /delete-driver oemNN.inf /uninstall    :: oemNN from `pnputil /enum-drivers`
bcdedit /set testsigning off                    :: when done developing
```

## Troubleshooting

- Use **WinDbg** + a kernel debugger connection; the driver logs through
  `DbgPrint`-style output you can extend.
- `!poolused DscA` (our tag `DISCROD_POOLTAG`) finds leaks.
- If the endpoints don't appear, re-check the interface GUIDs and reference
  strings in `DiscrodAudio.inf` against `src/common.h`.
