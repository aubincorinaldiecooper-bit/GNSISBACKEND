# @gnsis/ui — the GNSIS interface

The dock, the bar, the chats, the agent panels, settings and live voice, as
React components that do not know what is hosting them. The Electron desktop
(`desktop/src/renderer`) mounts them today; GNSISFRONTEND is meant to mount the
same code in the browser.

```
ui/
├─ src/index.ts          what a host imports: GnsisApp, the host contracts, the stores
├─ src/host.ts           LiveHost, IdentityStore, LiveEvent — the whole contract with a host
├─ src/GnsisApp.tsx      mount this once, hand it a host
├─ src/components/       Setup, Stage (the floating layer), Shell (dock ↔ bar), ChatWindow,
│                        AgentPanel, Overlays (menus, greeting, toast), Settings, Icons
├─ src/store/store.ts    one small store: conversations, tabs, presence, the live session
├─ src/store/live.ts     live voice as the chat sees it — pure functions, tested directly
├─ src/lib/              faces (blobatar), the public-ID derivation, small helpers
├─ src/hosts/            LocalIdentityStore (WebCrypto + IndexedDB), SimulatedLiveHost (no mic,
│                        no runtime: a scripted conversation, for review and tests)
├─ src/demo/data.ts      stand-in agents, panels and canned replies — see "What is stand-in"
├─ src/fonts/            Geist and Geist Mono, vendored (SIL OFL 1.1, licence alongside)
└─ dev/                  a browser preview with the simulated host
```

## Mounting it

```tsx
import { GnsisApp, LocalIdentityStore } from "@gnsis/ui";

<GnsisApp host={myHost} identity={new LocalIdentityStore()} demo={false} />
```

`GnsisApp` imports its own stylesheet, the fonts and blobatar's motion styles.
Bundle with anything that understands TypeScript, JSX and CSS/font imports
(esbuild and Vite both do). Peer dependencies: `react`, `react-dom` (18+).

## The host contract (`src/host.ts`)

A host implements `LiveHost`:

| The UI asks | The host does |
| --- | --- |
| `startLive()` | opens the microphone and starts a call; rejects with a plain sentence if it cannot |
| `endLive()` | closes the microphone and stops any reply still playing |
| `setMuted(bool)` | releases or reacquires the microphone only — the session stays alive |
| `startVision(source)` / `stopVision()` | points the one persistent visual sense at the screen or the camera, or off |
| `capabilities()` | `voice`, `screen`, `camera`, `transcript`, `overlay` — the UI hides what a host cannot do |
| `subscribe(fn)` | events below |

And it reports only what it actually observed:

| Event | Meaning |
| --- | --- |
| `link` | the connection to the runtime: connecting, ready, closed, error |
| `agent.text` | words the runtime is saying, in order, with `endOfTurn` / `interrupted` |
| `agent.speaking`, `agent.level` | real device playback and its level — never "audio was generated" |
| `agent.cut` | playback was cancelled (the person spoke over it, or asked) |
| `user.speech`, `user.level` | the person started/stopped talking, by microphone energy |
| `user.words` | the person's words as text, only when the host can transcribe |
| `mic`, `vision` | device state, with a plain-English `detail` when refused |

Because of this, the UI cannot show a reply that was never spoken, a
microphone that is not on, or words the person did not say: a spoken turn
without a transcript is shown as a spoken marker with its length.

`IdentityStore` keeps the device identity: `load`, `create`, `erase`, and one
sentence (`storageNote`) about where the private key lives, shown in Settings.
`LocalIdentityStore` keeps a non-extractable WebCrypto key in IndexedDB. It is
not the system keychain; the note says so.

## What is real and what is stand-in

Real, from the host: live voice (the runtime's words, playback, interruption,
mute, the person's turns), the visual sense, the device identity.

Stand-in, in `src/demo/data.ts`: the agents (Roofer prep, Recipe finder, …),
their panels, the approval card, the canned replies to typed messages, and
dictation (offered only when a host reports `transcript: true`). The runtime
has no text input and no agent backend yet; when it does, these are the seams
that change, and the shapes stay.

## Preview in a browser

```bash
cd desktop
npm run ui:preview          # builds dist/ui-preview/
npx serve dist/ui-preview   # then open it; add ?demo for the sample agents
```

The preview uses `SimulatedLiveHost`: no microphone, no runtime, a scripted
conversation. Serve it over http — IndexedDB, which keeps the identity, is not
available to a page opened from `file://` in a browser.

## Taking it into GNSISFRONTEND

The web app follows the same pattern it already uses for the runtime's live
page (`GNSIS/public/README.md` there): copy, do not fork.

1. Copy `desktop/ui/src` into the frontend (for example `GNSIS/src/gnsis-ui/`)
   whole, and `npm install blobatar@2.7.0 @blobatar/react@2.7.0`.
2. Write a browser `LiveHost` that opens `/ws/duplex` on the site's own origin
   (Caddy forwards it to the runtime) and drives the microphone, playback and
   capture the way `public/assets/live.js` does today. Words arrive as the
   runtime's `chunk` controls; the desktop host in
   `desktop/src/renderer/electronHost.ts` shows the mapping.
3. Mount `GnsisApp` on a route with that host and `LocalIdentityStore`.
4. Give the copy the same scoped exception the live surface has in the
   frontend's `AGENTS.md`: it is a copy of this package, styled by this
   package, and changes belong here first.

Changes to the interface go here, then the copy is refreshed wholesale.

## Rules for this package

- No Electron, Node, or bundler-specific imports. Web platform APIs only.
- Every claim the UI makes comes from a host event. Do not infer speaking from
  text, or mic state from a button press.
- Words the UI shows the person are plain English; error details from a host
  are sentences a person can act on, never raw engineering wording.
- Tests are pure: `store/live.ts` and the stores run under `node --test`
  without a DOM (`npm test` from `desktop/`).
