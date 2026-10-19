# Real-Mac acceptance record — overlay input fixes (#192, #193, this PR)

| Field | Value |
|---|---|
| Commit/build identity | PR #192 `1be4545` (hit:rects before createWindow, hit-report self-heal, DRAG_SLOP 10), PR #193 `c33b61e` (all IPC before first network await), this PR (model-menu `data-hit`); local `npm start` build of `desktop/` |
| Test environment | Real Mac (macOS 15 VM), Electron 39.8.10, floating overlay mode at 0.8 zoom, run directly via `node_modules/.bin/electron .` |
| Capability manifest identity | `desktop-v5` (`actions offered: open,files,browser,input,window,clipboard`) |
| Scenarios run | See table below |
| Routing latency | n/a — no execution path exercised (local-only run, runtime unreachable) |
| Executor latency | n/a — same |
| Verification latency | n/a — no model verification path exercised |
| Total latency | UI response to real clicks was immediate (menu/shell state changed within one frame tick; not instrumented — no hard threshold exists per AGENTS.md) |
| Confirmation wait | n/a — no confirmation-gated action exercised |
| Permission/TCC state | `microphone granted`, `camera granted`, `screen granted` (host log `permissions status` line at launch) |
| Playback cancel latency | n/a — no playback exercised |
| Resampler ratio/drift evidence | n/a — no audio-capture change in scope |
| Retries/fallbacks | `useHitRects` re-sends an unchanged report ~once per second so a dropped startup report self-heals |
| Duplicate/replay count | 0 — `setRects` is idempotent; repeated reports replace, never append |
| Failure layer | Startup IPC registration (main process) for #192/#193; renderer overlay markup (missing `data-hit`) for this PR |

## Scenarios (real clicks via the OS into the floating window)

| # | Scenario | Result |
|---|---|---|
| 1 | Launch → host log prints `floating window: the page reported N card(s) that take clicks` at startup | PASS (reported at ~350 ms after window ready, every launch) |
| 2 | Click dock face → dock expands into the bar | PASS |
| 3 | Click text field and type → characters land in the input | PASS |
| 4 | Enter sends the typed turn → chat window opens with the message and the plain-words not-connected line | PASS |
| 5 | Jittery press on an empty part of the bar (< ~8 screen px) → stays a click, cluster does not move, `gnsis.place` unchanged | PASS (was a real defect at DRAG_SLOP 5; verified place unchanged after fix) |
| 6 | `+` button → "What GNSIS can see" menu opens; "My screen" checks and the macOS screen picker appears; "Nothing" clears it | PASS (the VM's picker renders skeleton thumbnails — OS-side artifact, not app code) |
| 7 | Model picker button → menu opens | PASS (DOM toggle verified; see #8) |
| 8 | Click "GNSIS 0.5" in the open model menu | **FAIL on #193 build** — menu lacks `data-hit`, click fell through to the app beneath (Simulator took focus). **Fixed in this PR** by adding `data-hit` to `.model-menu` |
| 9 | Startup "No handler registered" errors for `link:state` / `media:permissions` / `model:preference` | PASS — zero after #193 (3+ per launch before) |
| 10 | Vision menu closes via its `+` toggle (outside clicks fall through by design on a transparent overlay) | PASS |
| 11 | `≡` menu (dock mode) opens via real click: Open chat, Settings, Hide to menu bar, Quit GNSIS | PASS |
| 12 | Settings opens via real click; Appearance tab; Glass Clear↔Dark applies (`gnsis-dark` class toggles); close button | PASS |
| 13 | "Hide to menu bar" → window shrinks/hides (`menu bar: tucked away`); menu-bar icon click → returns (`menu bar: come back`) | PASS — icon bounds verified via Accessibility API |
| 14 | Real drag on the face moves the cluster and persists `gnsis.place` | PASS (dx -151, dy -11 recorded) |
| 15 | Typing `/` in the bar opens the commands palette (`/screen` listed) | PASS |
| 16 | "Quit GNSIS" ends the app cleanly (`quit: from GNSIS's menu` → `will-quit`, process exits) | PASS |

## Notes

- Verification used a small CDP client against the renderer (`--remote-debugging-port=9222`) to read `[data-hit]` rects, inject `reportHitRects`, and screenshot the content-protected window, plus OS-level clicks.
- The root race (#192) was proven by: absent "page reported" log + zero `setRects` calls + manual `reportHitRects` injection restoring clicks on the buggy build.
- Remaining known limitation: the macOS ScreenCaptureKit picker renders skeleton thumbnails on this VM, so the final "pick a display" hop could not complete here; the request reaches the OS picker correctly.
