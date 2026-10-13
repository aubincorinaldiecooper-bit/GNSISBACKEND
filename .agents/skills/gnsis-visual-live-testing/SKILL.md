---
name: gnsis-visual-live-testing
description: Set up and diagnose live GNSIS visual-host testing with the Chrome Hub, Smaller GNSIS Modal service, and Linux desktop connector.
---

# GNSIS visual live testing

Use this when testing the visual host connectors against a real Smaller GNSIS deployment and the real GNSIS browser extension.

## Required environment

- Checkout with the connector code under test, normally `/home/ubuntu/repos/GNSISBACKEND-service`.
- Built extension output, normally `/home/ubuntu/repos/Gnsis-browser/packages/extension/.output/chrome-mv3`.
- Python environment with `sdks/python` installed, normally `/home/ubuntu/.venv-visual-ci-deps/bin/python`.
- `GNSIS_VISUAL_BASE_URL` for the deployed service.
- `GNSIS_VISUAL_HOST_TOKEN` read from a file or secret; never print it or include it in screenshots/reports.
- `GNSIS_VISUAL_TASK` for the desktop connector.

## Devin secrets needed

- GNSIS visual host token, usually supplied as a session file such as `/home/ubuntu/.devin-files/gnsis-visual-host-token`.

## Chrome Hub startup

1. Derive the extension ID from the manifest `key`; do not guess it.
2. If direct navigation to `chrome-extension://<id>/hub.html?ws=8766` returns `ERR_BLOCKED_BY_CLIENT`, use the real `OPEN_HUB` externally-connectable path from a localhost page:
   `chrome.runtime.sendMessage(extensionId, {type:'OPEN_HUB', wsPort:8766})`.
3. If actions fail with `Receiving end does not exist`, inspect the Chrome extension menu for the target origin. Setting the extension site access from `Ask on every visit` to `Always on all sites` and reloading the target enabled the real content-script channel in the October 2026 test environment.
4. If `capture.start` fails with `Extension has not been invoked for the current page`, re-check site access, reload the target, and try a delayed real connector session while focusing the target.

## Source dimensions matter

`browser.action` decisions must carry the real captured frame `source.width`/`source.height`, not screenshot or desktop-display dimensions. The bridge normalizes `decision.target` against `decision.viewport`. A wrong viewport can still produce a `browser.action.result` success while the page does not record the intended hit. Always validate with a page-side oracle (`window.eventLog`, title, or visible status marker).

In the October 2026 setup, captured frames were `1280×960` while the page viewport reported `3200×2217`. A source point must therefore be expressed in the captured-frame coordinate space. `resolve_target:false` reports the element under the mapped point; `resolve_target:true` additionally reports exact-hit bounds, but a reported exact hit is not sufficient proof of page-side mutation — inspect the oracle immediately afterward and before navigate/back actions can obscure state.

## Real-service diagnostics

- Check `GET /health` for policy, `active_sessions`, and metering counters before and after cleanup; treat any post-run count above the observed pre-run baseline as a possible leaked visual session.
- Browser connector success path should log `capture.started`, `capture.frame`, `capture.stopped`; then `/decisions` must return a decision before action dispatch.
- Modal log command: `modal app logs gnsis-visual`. Do not include tokens in fetched output; WebSocket URLs may include session tokens and should be sanitized before sharing.
- If `/decisions` fails, inspect the newest traceback. In the tested deployment, `TypeError: decode() got an unexpected keyword argument 'allowed_actions'` was a deployed service/runtime incompatibility, not a client auth or capture failure.
- Direct bridge protocol probes are useful to test policy/replay/stale-frame behavior in the real extension, but must be labeled as bridge evidence and not reported as model inference/action acceptance.

## Desktop notes

On Linux, ToolRegistry may offer only `files`, and production can agree to no executable tools. Do not claim click/type/browser execution or macOS permission behavior from Linux evidence. Record connector startup, real accepted screen frames, decision request, failure, session cleanup, and any subsequent screen reconnect/error loops.
