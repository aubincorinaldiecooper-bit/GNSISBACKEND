---
name: gnsis-visual-live-testing
description: Set up and diagnose live GNSIS visual-host testing with the Smaller GNSIS Modal service and the desktop connector.
---

# GNSIS visual live testing

Use this when testing the desktop visual connector against a real Smaller GNSIS deployment.

## Required environment

- Checkout with the connector code under test, normally `/home/ubuntu/repos/GNSISBACKEND-service`.
- Python environment with `sdks/python` installed, normally `/home/ubuntu/.venv-visual-ci-deps/bin/python`.
- `GNSIS_VISUAL_BASE_URL` for the deployed service.
- `GNSIS_VISUAL_HOST_TOKEN` read from a file or secret; never print it or include it in screenshots/reports.
- `GNSIS_VISUAL_TASK` for the desktop connector.

## Devin secrets needed

- GNSIS visual host token, usually supplied as a session file such as `/home/ubuntu/.devin-files/gnsis-visual-host-token`.

## Real-service diagnostics

- Check `GET /health` for policy, `active_sessions`, and metering counters before and after cleanup; treat any post-run count above the observed pre-run baseline as a possible leaked visual session.
- `/decisions` must return a decision before any action dispatch.
- Modal log command: `modal app logs gnsis-visual`. Do not include tokens in fetched output; WebSocket URLs may include session tokens and should be sanitized before sharing.
- If `/decisions` fails, inspect the newest traceback. In the tested deployment, `TypeError: decode() got an unexpected keyword argument 'allowed_actions'` was a deployed service/runtime incompatibility, not a client auth or capture failure.

## Desktop notes

On Linux, ToolRegistry may offer only `files`, and production can agree to no executable tools. Do not claim click/type/browser execution or macOS permission behavior from Linux evidence. Record connector startup, real accepted screen frames, decision request, failure, session cleanup, and any subsequent screen reconnect/error loops.
