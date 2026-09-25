# GNSIS persistent-visual-sense audit

Audit of the desktop + runtime visual architecture against the product rule:

> GNSIS has one persistent visual sense. Screen and camera are continuously
> captured through the live visual pipeline, and every still image used for
> perception must come from that same persistent stream. GNSIS must not
> maintain a separate screenshot-based perception path.

**Verdict: PARTIALLY ALIGNED.** The live screen/camera path is a genuinely
persistent stream end to end — that core is correct and must be preserved.
Against the rule there is one piece of real architectural drift (a second,
independent screenshot path that is wired, callable, and advertised but
unused) plus three smaller gaps: the desktop sampler is heavier than the
production browser path it was modeled on, visual history is thin and
conditional, and the screen socket silently drops frames instead of
recovering.

---

## 1. The real live visual path today

```
renderer app.ts shareFrames(source)
  getUserMedia({video}) | getDisplayMedia({video})        stream acquired ONCE
  MediaStreamTrack (persistent, until track.onended)
        |
        v setInterval(grab, 1000)                         1 Hz, hardcoded
  new ImageCapture(track) EVERY tick                     recreated per frame
  grabFrame() -> canvas -> toBlob(image/jpeg, 0.7)       full source resolution
  captured_at_ms = Date.now()                            wall-clock, pre-encode
        |
        v IPC "screen:frame" (preload gnsis.sendScreenFrame)
  main.ts -> HostSession.sendScreenFrame(ScreenFrameMetadata)
        |
        v ScreenClient.sendFrame -> /ws/screen
  JSON metadata then binary payload                      frame_id: crypto.randomUUID
        |
        v runtime online_duplex.py /ws/screen
  ScreenFrameHeader.from_payload   type must be "screen.frame",
                                   video_source in {"screen","camera"}
  decode_screen_frame              PIL, bounded by max_frame_bytes / max_pixels
  codex_frame_gate.accept          rate gate: one frame per
                                   chunk_ms / codex_frame_rate_multiplier
  active.duplex.enqueue_screen_frame -> LatestScreenFrameBuffer.publish
  ACK "screen.frame.accepted" (+ context_sampled)
        |
        v front brain
  DuplexLiveSession._frames_for_unit -> consume_for_unit(
      reuse_base = media_mode == "omni",
      captured_not_after_ms)        at the model-unit clock
        |
        v MiniCPM unit prefill (image tokens)
        |
        v back brain (conditional)
  if coordinator AND context_sampled AND persist policy:
      persist_screen_frame -> MediaRef(kind="screen"|"frame")
      -> coordinator.remember_media -> _recent_media deque
      -> realtime context event -> turn.final media (8 s window)
```

Stage detail:

| stage | file | API | notes |
|---|---|---|---|
| acquire | `desktop/src/renderer/app.ts` `shareFrames` | `getUserMedia` / `getDisplayMedia` | acquired once per source activation; stays open until track end |
| sample | `app.ts` `grab`/`setInterval` | `ImageCapture.grabFrame` → canvas `toBlob` | 1 Hz; `ImageCapture` rebuilt every tick; full resolution |
| encode | same | JPEG q0.7 | no size cap (production `video.js` fits 448 px) |
| IPC | `desktop/src/preload.ts`, `main.ts` | `screen:frame` | typed `ScreenFrameMetadata` both sides |
| session | `desktop/src/host/hostSession.ts` | `sendScreenFrame` | socket-owner, chassis-neutral |
| wire | `desktop/src/main/wsClient.ts` `ScreenClient` | `/ws/screen` JSON+binary | **no outbox, no reconnect — frames dropped silently while closed** |
| validate/decode | `runtime/.../screen_transport.py` | `ScreenFrameHeader.from_payload`, `decode_screen_frame` | bounds enforced; `video_source` validated |
| gate/buffer | `runtime/.../online_duplex.py`, `screen.py` | `ScreenFrameRateGate`, `LatestScreenFrameBuffer` | rate gate; capture-ordered buffer, `_last` reuse |
| model | `runtime/minicpm_ft/mcpmft/infer/realtime.py` | `_frames_for_unit` → `consume_for_unit` | newest eligible frame per model unit; omni mode reuses last |
| persist/context | `online_duplex.py`, `task_tools_online.py` | `persist_screen_frame` → `remember_media` → `_recent_media` | only when a coordinator exists and policy allows |

**So: it is a real stream, not recreated screenshots.** Capture is one
persistent `MediaStream`; frames are sampled from it. What must be noted is
that *the stream is not resilient*: `ScreenClient.sendFrame` returns silently
when the socket is closed, and `track.onended` just clears the interval. The
production browser client (`mcpmft/infer/static/video.js`) already separates
capture and transport state machines — "capture is never reacquired to
repair transport" — with a reconnect budget; the desktop does not.

## 2. The separate screenshot path

Every reference to `ScreenshotAdapter` / `ElectronScreenshots` /
`captureScreenshot` / `screenshot:capture` / `screenshots` /
`desktopCapturer.getSources`:

| location | role | callable? | caller? | perception? |
|---|---|---|---|---|
| `desktop/src/host/adapters.ts` `ScreenshotAdapter` + `HostAdapters.screenshots` | interface + adapter slot | yes (contract) | `main.ts` only | would be, if used |
| `desktop/src/host/electronMain.ts` `ElectronScreenshots` | `desktopCapturer.getSources` → `thumbnail.toPNG()` | yes | IPC handler | yes, independent truth |
| `desktop/src/main/main.ts` | instantiation (l.55), `screenshots: true` capability (l.70), `ipcMain.handle("screenshot:capture")` (l.118) | yes | wires it | n/a |
| `desktop/src/preload.ts` `gnsis.captureScreenshot` | exposes to renderer | yes | none | n/a |
| `desktop/src/renderer/app.ts` l.13 | type declaration | — | **never called** | n/a |
| `desktop/src/host/protocol.ts` l.22 `HostCapabilities.screenshots: boolean` | advertised to the daemon | yes | `host.ready` | claims a capability |
| `docs/desktop_chassis_decision.md` l.22, 41; `docs/desktop_execution_agent.md` l.31, 64, 114 | "native screenshots" inherited from the Qwen Host feature list | — | docs | justifies it |

Answers in order: **callable** (IPC is live end to end) — **called by
nobody** — **not used by perception today** — **not exposed as a tool** —
**yes, it produces visual state independently** (a separate OS capture of
the whole display at thumbnail resolution, uncorrelated with the stream's
frame_ids/timestamps) — **nothing depends on it**.

`desktopCapturer.getSources` also acquires screen sources through a
*different* API than the live path's `getDisplayMedia`, so it can literally
return a different monitor than the one being streamed. This is a second
source of visual truth: **architectural drift**, per the hard rule — even
though it is dead code, it is a live, advertised perception path that any
future feature (or a ported Qwen Host tool) could reach for.

## 3. Can `ScreenshotAdapter` be removed?

Yes — removal is a deletion, not a redesign. Complete dependency map:

- `desktop/src/host/adapters.ts` — delete `ScreenshotAdapter` interface and
  the `screenshots` field of `HostAdapters`.
- `desktop/src/host/electronMain.ts` — delete `ElectronScreenshots` and the
  `desktopCapturer` import.
- `desktop/src/main/main.ts` — delete the `ElectronScreenshots` import, the
  `screenshots` const, `ipcMain.handle("screenshot:capture")`, and
  `capabilities.screenshots: true`.
- `desktop/src/preload.ts` — delete `captureScreenshot`.
- `desktop/src/renderer/app.ts` — delete the `captureScreenshot` type member.
- `desktop/src/host/protocol.ts` — delete `screenshots` from
  `HostCapabilities`. Note: `HOST_PROTOCOL_VERSION` exists; the flag is a
  capability advertisement, not a wire-required field, so removal needs no
  version bump — the daemon tolerates absent capabilities.
- `docs/desktop_chassis_decision.md`, `docs/desktop_execution_agent.md` —
  strike "native screenshots" from the carried-over Qwen Host feature lists
  (or annotate: intentionally not carried over — one persistent visual
  sense).
- No tests reference it. Nothing in `src/`, `tools/`, or `runtime/` knows
  the IPC channel exists.

## 4. Where still-frame inspection should come from

Everything should come off the stream, and the pieces already exist — they
just aren't queryable today:

- **Latest frame** — `LatestScreenFrameBuffer._last` already retains the
  newest consumed frame (and omni mode reuses it). Smallest API: read it,
  don't add anything.
- **A frame from a few seconds ago / a timestamp** — needs a retained window
  the front brain doesn't currently keep (see §5).
- **Several recent frames** — same retained window; the back brain's
  `_recent_media` deque is the closest existing mechanism but is conditional
  on a coordinator and persist policy.
- **Higher-quality version of current visual state** — the encoded payload
  is already persisted to disk when policy allows
  (`persist_screen_frame`, `MediaRef` path) — a back-brain can re-read the
  file. For the front brain, the right seam is "ask the buffer for the
  newest frame," not "re-shoot the screen."

Recommended smallest design: **one rolling, timestamp-indexed recent-frame
retention next to `LatestScreenFrameBuffer`** (seconds-bounded, e.g. the
existing `codex_screen_history_seconds = 8 s` window as the default
horizon), shared by both consumption paths — front-brain unit prefill and
back-brain `remember_media`. That gives `latest`, `at(t)`, `last(n)`
uniformly. A separate "pin frame" or "high-res observation" API is *not*
needed yet: pinned = retained MediaRef, high-res = the persisted original.
Add them only when a caller exists.

## 5. Temporal visual continuity

Concrete answers: "what changed?" — only implicitly, via model KV context
consuming consecutive units. "What was there two seconds ago?" — only if the
frame is still pending in `LatestScreenFrameBuffer` (unconsumed) **or**
persisted to `_recent_media` (coordinator present + `persist_camera_frames`
/ persist policy + inside the 8 s `screen_context_window_ms`). "Where did
that object move?" — the model reasons over consumed units; there is no
re-queryable pixel history. "What happened just before this?" — the shared
timeline records events and `remember_media` records context refs, but
frames are not retained for lookup.

**Where recent visual history lives:** pending frames —
`LatestScreenFrameBuffer` (runtime, bounded `context_max_units`); last
consumed — `_last` (runtime); persisted originals — `media_dir/<session>/screen/`
(conditional); recent refs — `_recent_media` deque (conditional, 8 s
ephemeral). Nowhere is there a guaranteed, queryable rolling history keyed
by `captured_at_ms`.

**Smallest addition:** retain consumed frames for a bounded window (same
buffer class or a sibling ring keyed by capture time), so the daemon can
answer latest/at(t)/last(n) without the client doing anything new. No wire
change required — a daemon-side data-structure change.

## 6. Frame-sampling mechanism audit

| question | answer |
|---|---|
| Stream acquired once, kept alive? | Yes — one `MediaStream`/`track` per source activation. |
| `ImageCapture` unnecessarily recreated? | **Yes — `new ImageCapture(track)` inside every 1 s tick** (`app.ts` l.136-140). Should be hoisted, or better: mirror production `video.js`, which draws the `MediaStream` through a hidden `<video>` element + `drawImage` — no `ImageCapture` at all. |
| Avoidable latency/copies? | `grabFrame()` → `ImageBitmap` → canvas → `toBlob` → `arrayBuffer` per frame; the video-element path skips the grabFrame promise entirely. Also, desktop sends **full-resolution** JPEG while production fits 448 px — larger encode + wire cost per frame. |
| Is 1 Hz a model/runtime constraint? | Not hardcoded semantics — the runtime *advertises* `recommended_frame_rate` (= model unit rate) and `codex_frame_rate` in `screen.ready`; production `video.js` honors it. The desktop hardcodes 1000 ms instead of reading the recommendation. |
| `requestVideoFrameCallback` / WebCodecs / native callbacks — materially better? | Marginal. `requestVideoFrameCallback` would align capture with produced frames (avoids capturing stale intervals); WebCodecs adds complexity for little gain at ~1 Hz. **Do not replace working tech without a measured reason** — the baseline harness (`desktop/src/bench/bench.ts`) exists to measure it first. |

The distinction the rule draws holds: sampling frames from one persistent
stream = acceptable (what both clients do); independently capturing
screenshots = unacceptable (what `ElectronScreenshots` is).

## 7. Camera and screen: one perception architecture?

Beyond the protocol layer (unified by PR #107), yes:

- **Lifecycle** — same `shareFrames` function, same socket, same session.
- **Timestamps** — same `captured_at_ms` wall-clock contract.
- **Buffering** — same `LatestScreenFrameBuffer`; model consumption
  identical (`_frames_for_unit` doesn't branch on source).
- **Transport** — same `/ws/screen`, same header validation, same ACKs.
- **Divergence (intentional, not parallel)**: persistence `kind` is tagged
  `screen` vs `frame` (camera); `persist_camera_frames` policy flag; source
  falls back to session `video_source` then `"camera"`.
- **Gaps shared by both**: silent frame drop on closed socket, no reconnect
  — the 1 Hz hardcode and full-res encode also apply equally to both
  sources.

No hidden parallel implementation. The unification holds.

## 8. Screenshot-style architecture elsewhere

Repo-wide classification of `screenshot`/`snapshot`/`desktopCapturer`/etc.:

- **All "snapshot" hits** (`coordination.py`, `gateway.py`, `cli.py`,
  `deferred_app.py`, `startup_timing.py`, `codex_coordinator.py`) —
  coordinator/resource snapshots. *Not visual. Live, correct.*
- **`mcpmft/infer/static/video.js`** — live perception, same persistent
  stream pattern; actually the *better* implementation of it.
- **`video_frame` context kind** (`contracts.py`,
  `EPHEMERAL_VISUAL_CONTEXT_KINDS`, `task_tools_online.py`,
  `worker_tools.py`) — internal ledger kind for camera/persisted frames,
  not a wire type. *Live, aligned.*
- **Venus provider `/video_frame` endpoint** (`providers/venus.py`) —
  upstream model-server contract. *Live, provider-internal.*
- **Desktop screenshot surface** (`ScreenshotAdapter` family) — *the only
  drift*; §2.
- No computer-use image path, vision tool, or thumbnail pipeline found
  anywhere else.

## Required conclusion

**PARTIALLY ALIGNED.** GNSIS's live visual sense is a genuine persistent
stream — but a second screenshot-based path exists on the same desktop
(alive, callable, advertised, uncalled), and the retained-history story is
too thin to serve still-frame queries without reacquisition in the general
case.

1. **Is capture genuinely persistent?** Yes — one `MediaStream` per source,
   sampled continuously; never reacquired per frame.
2. **Does a second screenshot path still exist?** Yes — `ElectronScreenshots`
   → `screenshot:capture` → `gnsis.captureScreenshot`, plus the
   `screenshots: true` capability claim. Unused but live drift.
3. **Can `ScreenshotAdapter` be removed safely?** Yes — single-branch
   deletion; no callers, no dependents (map in §3).
4. **Does GNSIS retain enough recent visual history?** No — pending-only
   buffer + conditional 8 s `_recent_media`; nothing queryable by
   `captured_at_ms` unconditionally.
5. **Smallest architecture for all stills from the stream?** A bounded,
   timestamp-indexed rolling recent-frame retention beside
   `LatestScreenFrameBuffer` (default horizon = the existing
   `codex_screen_history_seconds`), read by front-brain reuse and
   back-brain `remember_media`; stills/higher-quality come from already
   persisted `MediaRef` files. No screenshot API, no new wire protocol.
6. **PR sequence** — below.

## Ordered PR plan

1. **PR A — remove the screenshot surface** (desktop, small):
   delete the §3 map end to end; correct the two chassis docs.
2. **PR B — align the desktop sampler with production** (desktop, small):
   hoist/`ImageCapture`→video-element `drawImage` per `video.js`; honor
   `screen.ready.recommended_frame_rate` instead of hardcoded 1 Hz; FIT cap;
   give `ScreenClient` the capture/transport split + reconnect budget
   `video.js` already proves. Baseline-measure first with `bench.ts` —
   tune, don't rewrite.
3. **PR C — daemon-side rolling visual history** (runtime, medium):
   bounded timestamp-indexed recent-frame retention wired into
   `LatestScreenFrameBuffer`/coordinator path; expose latest/at(t)/last(n)
   internally; decide persistence policy per kind.
4. **PR D — still-inspection contract** (only if a caller emerges):
   a daemon query served from retained frames — never by reacquiring the
   screen. Defer until a concrete consumer exists.

## Risks

- Removing the `screenshots` capability changes `host.ready` payload —
  low risk (capabilities are advisory), flag it in the PR.
- Rate change (B) alters model-unit/frame alignment — measure against
  `codex_frame_gate` before landing; the gate already protects the model.
- Retained history (C) is memory cost — bound by seconds and frames, not
  "keep everything"; honor the same persist/privacy policy as
  `persist_camera_frames` (camera frames are opt-in today for a reason).

## Acceptance criteria

- `rg "desktopCapturer|ScreenshotAdapter|screenshot:capture|captureScreenshot|screenshots: true"` → no live hits.
- All perception stills trace to `screen.frame` frames on `/ws/screen`.
- `latest`/`at(t)` answerable from retained stream frames without any
  client-side recapture.
- Camera and screen continue to share one code path end to end.
- Desktop tests/typecheck/build + runtime suite stay green; no daemon
  behavior changed to accommodate a client-side contract.
