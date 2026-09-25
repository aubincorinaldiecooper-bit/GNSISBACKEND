# Packaging GNSIS for macOS

Produces an installable `GNSIS-<version>-<arch>.dmg` from the existing Electron
Host. The packaged app is the same code as `npm start` — packaging adds no
architecture, only distribution.

## Build

```bash
cd desktop
npm ci
npm run package:mac        # GNSIS-<version>-arm64.dmg in release/
npm run package:mac:dir    # unpacked GNSIS.app only (faster smoke check)
```

Artifact: `desktop/release/GNSIS-<version>-arm64.dmg`.

The DMG must be built on macOS (`hdiutil`). Linux CI builds everything up to
the `.app`; the `.dmg` step runs on the `macos` runner in
`.github/workflows/desktop-dmg.yml`.

## Identity

- Product name: `GNSIS`
- Bundle id: `com.gnsis.desktop`
- Version source: `desktop/package.json` `version`

## Runtime configuration

The packaged app resolves the runtime in this order:

1. `GNSIS_RUNTIME_URL` environment variable (dev/CI);
2. `<userData>/gnsis.json` — `{"runtimeUrl": "http://127.0.0.1:8080"}`
   (`~/Library/Application Support/GNSIS/gnsis.json` on macOS);
3. `http://127.0.0.1:8080`.

The JSON file is the supported seam for pointing an installed build at a
remote runtime today and at `LocalGNSISProvider` later — no repackaging.

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

## What's inside the package

Only `dist/` (the bundled app) + `package.json`. No tests, sources,
sourcemaps, model assets, or dev infrastructure. `release/` is the output
dir for packaged artifacts.

## macOS permissions

On first use the installed app requests microphone, camera (when used), and
screen recording through the usual macOS prompts. Denied permission surfaces
as a state, not a crash — other functionality remains usable.
