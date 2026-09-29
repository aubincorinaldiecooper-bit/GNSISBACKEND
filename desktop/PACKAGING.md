# Packaging GNSIS for macOS

Produces an installable `GNSIS-<version>-<arch>.dmg` from the existing Electron
Host. The packaged app is the same code as `npm start` — packaging adds no
architecture, only distribution.

## Build

```bash
cd desktop
npm ci
npm run package:mac        # GNSIS-<version>-universal.dmg in release/
npm run package:mac:dir    # unpacked GNSIS.app only (faster smoke check)
```

Artifact: `desktop/release/GNSIS-<version>-universal.dmg` — a universal app
that runs natively on Apple Silicon and Intel; users never pick a CPU
architecture.

The DMG must be built on macOS (`hdiutil`). Linux CI builds everything up to
the `.app`; the `.dmg` step runs on the `macos` runner in
`.github/workflows/desktop-dmg.yml`, which also verifies every Mach-O in the
bundle carries both `x86_64` and `arm64` slices (`lipo -info`) before the
artifact uploads.

## Identity

- Product name: `GNSIS`
- Bundle id: `com.gnsis.desktop`
- Version source: `desktop/package.json` `version`

## Runtime configuration

The packaged app resolves the runtime in this order:

1. `GNSIS_RUNTIME_URL` environment variable (dev/CI);
2. `<userData>/gnsis.json` — optional developer/local override
   (`~/Library/Application Support/GNSIS/gnsis.json` on macOS);
3. `https://gnsis.studio`.

A normal user does not configure a runtime. A fresh install connects to
`https://gnsis.studio` automatically. The environment variable and JSON file
remain only as supported seams for development, testing, and the future
`LocalGNSISProvider`.

Production traffic goes through the site, not the runtime's own
`.modal.run` address. The
site forwards `/ws/duplex` and `/ws/screen` and adds the two things the
runtime requires and the app does not hold: its front-door secret
(`X-GNSIS-Edge`) and the Modal proxy credentials. A direct connection to the
runtime's address is refused.

`GNSIS_DEMO=1` opens the interface with the sample agents loaded, so every
state can be reviewed in the packaged app (Settings → Developer → Load demo
agents does the same at run time).

## Signing / notarization

- Development DMG (default): run with `CSC_IDENTITY_AUTO_DISCOVERY=false` —
  produces an **unsigned** artifact. Install requires the usual
  unsigned-app ritual (right-click → Open, or `xattr -dr com.apple.quarantine`).
- Distribution: set `CSC_LINK` / `CSC_KEY_PASSWORD` (Developer ID
  Application certificate) and `APPLE_API_KEY` / `APPLE_API_KEY_ID` /
  `APPLE_API_ISSUER` (App Store Connect API key) in the environment —
  electron-builder signs with hardened runtime and notarizes automatically.
- Never commit `.p12` files, private keys, Apple passwords, or API keys.

Current status: `signed: no` / `notarized: no` unless the above credentials
are configured.

## Entitlements (hardened runtime)

`build/entitlements.mac.plist` — used only when signing:

- `allow-jit` / `allow-unsigned-executable-memory` /
  `allow-dyld-environment-variables` — V8 requirements;
- `disable-library-validation` — Electron ships unsigned dylibs;
- `device.audio-input` / `device.camera` — mic/camera access. Screen
  recording is TCC user consent (Info.plist usage description), not an
  entitlement.
- `automation.apple-events` — lets GNSIS ask to control Finder, the browser
  and System Events for actions. macOS still asks the person once per app;
  `NSAppleEventsUsageDescription` is the sentence that prompt shows.

## What's inside the package

Only `dist/` (the bundled app) + `package.json`. No tests, sources,
sourcemaps, model assets, or dev infrastructure. `release/` is the output
dir for packaged artifacts.

The renderer in `dist/renderer/` is the product interface, `@gnsis/ui`
(`ui/`, an npm workspace of this package), bundled by esbuild with React, the
Geist fonts it ships, and blobatar for the faces. Nothing in it is fetched at
run time: the page's content policy allows only its own files. See
`ui/README.md` for the interface and how GNSISFRONTEND takes the same code.

## macOS permissions

On first use the installed app requests microphone, camera (when used), and
screen recording through the usual macOS prompts. Denied permission surfaces
as a state, not a crash — other functionality remains usable.

## Actions on the Mac

Browser use requires **no Chrome extension and no Chrome Web Store install**.
GNSIS uses the browser the person already has open: the shared screen stream is
the visual source, browser automation owns tabs/navigation, and the native input
tool handles clicks, typing and keys under macOS Accessibility permission.

When the model asks GNSIS to do something — open an app or a site, move or
rename a file, use the open browser, type or click — the runtime sends it to
this app, which does it and answers. The four tools are listed in
`runtime/configs/gnsis-host-tools.json`; the runtime shows the model only the
ones this app offers when it connects, and this app offers them only to
`https://gnsis.studio` or a runtime on the same machine. `"actions": false`
in `gnsis.json` (or `GNSIS_ACTIONS=off`) turns them off; `"actions": true`
allows them for another runtime address the person chose.

What macOS will ask, the first time each is needed:

| When GNSIS first… | macOS asks | Where to change it later |
| --- | --- | --- |
| uses what is selected in Finder, or the open Finder window | “GNSIS wants to control Finder” | Privacy & Security → Automation |
| uses the browser (tabs, go to an address) | “GNSIS wants to control Google Chrome / Safari / …”, once per browser | Privacy & Security → Automation |
| types, presses keys or clicks | GNSIS must be switched on under Accessibility; GNSIS opens that prompt and says so | Privacy & Security → Accessibility |

A refusal is never silent: the model is told which permission is missing and
where it is, and says so. Opening apps, sites, files and folders, and
listing, finding, moving and renaming files need none of these.

When GNSIS asks before acting: anything that changes files, types or clicks,
or loads a web address runs straight away only when the person's own
transcribed words name what it acts on. The desktop sends each thing the
person says to the runtime's speech-to-text (`/api/asr/transcribe`) and hands
the words back as `turn.final`; only turns the runtime accepts count. The
production runtime has speech-to-text off (`asr.mode: disabled`), so there are
no such words and those actions show an Allow / Don't Allow alert first; the
host log says `no speech-to-text at the runtime` once. Quitting apps, emptying
the Trash, sending, buying or deleting are always asked about. Deleting,
moving to the Trash and replacing files are never done, and a folder that is
really a link into a hidden, system or outside place is treated as that place.

### Checking it on a Mac

1. Deploy the runtime from this commit (`modal deploy modal/gnsis_voice.py`,
   or the worker's deploy route), then run `scripts/verify-modal-gnsis.py`:
   `/health` lists `host_tools` with version `desktop-v2`.
2. Install the DMG from this commit's `desktop-dmg` run and start it. A
   fresh install connects to `https://gnsis.studio` automatically. The host log
   (`~/Library/Application Support/GNSIS/logs/gnsis-host.log`) shows
   `actions offered: open,files,browser,input` and `tools agreed with runtime:`
   with the same four.
3. Share the screen, start live voice, and say “Move that file into the
   Projects folder” with a file selected in Finder. Expect: an Allow alert
   naming the file and the folder, the file in Projects, a “Done: Moved …”
   line in the chat, and GNSIS saying it is done.
4. “Open example.com and find the word domain”: an alert for example.com,
   the page in the browser, then GNSIS looking and answering.
5. In the host log every step carries the call id: `call … files → done`,
   and the runtime's timeline has `tool.requested` → `action.*` →
   `tool.response.injected` for the same id.
