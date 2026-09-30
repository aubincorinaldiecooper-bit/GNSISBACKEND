import { useEffect, useState, type ReactNode } from "react";
import { Face } from "../lib/face";
import { shareLink } from "../lib/identity";
import { copyText } from "../lib/platform";
import { actions, eraseIdentity, getHost, getIdentityStore, setState, useStore } from "../store/store";
import * as I from "./Icons";

type Section = "profile" | "voice" | "agents" | "notif" | "appear" | "privacy";
const SECTIONS: { id: Section; label: string }[] = [
  { id: "profile", label: "Profile & sharing" },
  { id: "voice", label: "Voice" },
  { id: "agents", label: "Agents" },
  { id: "notif", label: "Notifications" },
  { id: "appear", label: "Appearance" },
  { id: "privacy", label: "Privacy & security" },
];

export function Settings() {
  const identity = useStore((s) => s.identity);
  const caps = useStore((s) => s.caps);
  // Stand-in agents exist only in demo mode; the real app never offers them.
  const demo = useStore((s) => s.demo);
  // These switches are the product's intended settings, and nothing reads
  // them yet. In the real app they are shown but switched off, so none of them
  // looks like it controls what GNSIS does. Demo mode keeps them live for review.
  const inert = !demo;
  const notYet = inert ? NOT_YET : undefined;
  const [eraseNote, setEraseNote] = useState("");
  const [section, setSection] = useState<Section>("profile");
  const [tg, setTg] = useState({ findable: true, autosend: true, speak: false, approve: true, browser: true, nFinish: true, nNeeds: true, nSound: false, motion: false });
  const [copied, setCopied] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [dock, setDock] = useState<"bottom" | "top">("bottom");
  const [glass, setGlass] = useState(55);
  // Esc closes Settings, as it would any panel: floating, it sits over other apps.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) setState({ settingsOpen: false });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (!identity) return null;
  const link = shareLink(identity);
  const keyNote = getIdentityStore()?.storageNote ?? "";
  const flip = (k: keyof typeof tg) => setTg((x) => ({ ...x, [k]: !x[k] }));
  const copy = async (which: string, text: string) => {
    if (await copyText(text)) {
      setCopied(which);
      window.setTimeout(() => setCopied(null), 1800);
    }
  };
  const share = async () => {
    if (navigator.share) {
      try { await navigator.share({ title: "My GNSIS", url: link }); return; } catch { /* cancelled */ }
    }
    await copy("link", link);
    setNote("Sharing isn’t available here, so the link was copied instead.");
  };
  const toggle = (k: keyof typeof tg, label: string) => (
    <button type="button" role="switch" aria-label={label} aria-checked={tg[k]} className={"switch" + (tg[k] ? " on" : "")} disabled={inert} title={notYet} onClick={() => flip(k)}><span /></button>
  );

  return (
    <div className="settings-scrim">
      <div data-hit role="dialog" aria-label="Settings" className="settings glass">
        <nav className="settings-nav">
          <div className="settings-brand"><Face name={identity.publicId} gnsis size={34} /> Settings</div>
          {SECTIONS.map((x) => (
            <button key={x.id} type="button" aria-current={section === x.id} className={section === x.id ? "on" : ""} onClick={() => setSection(x.id)}>{x.label}</button>
          ))}
        </nav>
        <div className="settings-main">
          <header>
            <h1>{SECTIONS.find((x) => x.id === section)!.label}</h1>
            <button type="button" className="icon-btn" aria-label="Close settings" onClick={() => setState({ settingsOpen: false })}><I.Close /></button>
          </header>
          <div className="settings-body">
            {section === "profile" && (
              <>
                <div className="card row-card"><Face name={identity.publicId} gnsis size={84} /><div><h2>Your GNSIS</h2><p className="muted">Created on this computer. Your private key never leaves it.</p></div></div>
                <Group title="Sharing" note={inert ? NOT_YET_NOTE : undefined}>
                  <div className="field-block">
                    <Label title="Public ID" desc="Safe to share. People can use it to find and message your GNSIS. It never reveals your private key." />
                    <div className="field-row">
                      <input readOnly aria-label="Public ID" className="mono-field" value={tg.findable ? identity.publicId : "Hidden while you’re private"} />
                      <button type="button" className="btn-secondary" onClick={() => copy("id", identity.publicId)}><I.Copy size={18} /> {copied === "id" ? "Copied" : "Copy"}</button>
                    </div>
                  </div>
                  <div className="field-block">
                    <Label title="Share link" desc="Post it anywhere. Share opens your computer’s share menu, so you can send it to your social apps." />
                    <div className="field-row">
                      <input readOnly aria-label="Share link" className="mono-field" value={tg.findable ? link.replace("https://", "") : "Turned off"} />
                      <button type="button" className="btn-secondary" onClick={() => copy("link", link)}><I.Copy size={18} /> {copied === "link" ? "Copied" : "Copy link"}</button>
                      <button type="button" className="btn-dark" onClick={share}><I.Share size={18} /> Share…</button>
                    </div>
                    <div className="muted small" aria-live="polite">{note}</div>
                  </div>
                  <Row title="Let people find me by my public ID" desc="Turn off to stay private. Your link stops working until you turn it back on.">{toggle("findable", "Let people find me by my public ID")}</Row>
                </Group>
              </>
            )}
            {section === "voice" && (
              <Group note={inert ? NOT_YET_NOTE : undefined}>
                <Row title="Wake gesture" desc="Starts or ends live voice with whoever is in front, same as the voice button. There is no hardware gesture yet; ⌥Space stands in while GNSIS is focused."><span className="chip">⌥Space</span></Row>
                <Row title="Microphone"><Select label="Microphone" options={["Built-in microphone", "External microphone"]} disabled={inert} /></Row>
                {caps.transcript && <Row title="Send when I stop talking" desc="For dictation. Sends after a short pause.">{toggle("autosend", "Send when I stop talking")}</Row>}
                <Row title="Read replies out loud">{toggle("speak", "Read replies out loud")}</Row>
                <Row title="Voice"><Select label="Voice" options={["Calm", "Bright", "Deep"]} disabled={inert} /></Row>
              </Group>
            )}
            {section === "agents" && (
              <Group note={inert ? NOT_YET_NOTE : undefined}>
                <Row title="Ask before sending emails or messages" desc="Agents show you an approval card first.">{toggle("approve", "Ask before sending emails or messages")}</Row>
                <Row title="Let agents use a browser" desc="Needed for tasks like finding a recipe or filling out a form.">{toggle("browser", "Let agents use a browser")}</Row>
                <Row title="Remove finished agents from the dock" desc="They stay in All agents."><Select label="Remove finished agents from the dock" options={["After 10 minutes", "After 1 hour", "After 1 day", "Never"]} disabled={inert} /></Row>
              </Group>
            )}
            {section === "notif" && (
              <Group note={inert ? NOT_YET_NOTE : undefined}>
                <Row title="When a background agent finishes">{toggle("nFinish", "When a background agent finishes")}</Row>
                <Row title="When an agent needs you">{toggle("nNeeds", "When an agent needs you")}</Row>
                <Row title="Play a sound">{toggle("nSound", "Play a sound")}</Row>
              </Group>
            )}
            {section === "appear" && (
              <Group note={inert ? NOT_YET_NOTE : undefined}>
                <Row title="Glass transparency"><input type="range" aria-label="Glass transparency" min={20} max={85} value={glass} disabled={inert} title={notYet} onChange={(e) => setGlass(Number(e.target.value))} /></Row>
                <Row title="Dock position">
                  <div className="segmented" role="radiogroup" aria-label="Dock position">
                    {(["bottom", "top"] as const).map((d) => <button key={d} type="button" role="radio" aria-checked={dock === d} className={dock === d ? "on" : ""} disabled={inert} title={notYet} onClick={() => setDock(d)}>{d === "bottom" ? "Bottom" : "Top"}</button>)}
                  </div>
                </Row>
                <Row title="Reduce motion" desc="Turns off breathing faces and sliding windows.">{toggle("motion", "Reduce motion")}</Row>
              </Group>
            )}
            {section === "privacy" && (
              <>
                <Group note={NOT_YET_NOTE}>
                  <Row title="Private key" desc={keyNote}><span className="chip green">{identity.storage === "keychain" ? "In this Mac’s Keychain" : "On this device"}</span></Row>
                  <Row title="Back up your key" desc="If this computer is lost, a backup is the only way to get your GNSIS back."><button type="button" className="btn-secondary" disabled title="Not in this build yet">Back up…</button></Row>
                  <Row title="Conversation history" desc="This app doesn’t keep the chat: it clears when GNSIS closes.">{null}</Row>
                </Group>
                {demo && (
                  <Group title="Developer">
                    <Row title="Load demo agents" desc="Fills the dock with sample agents so every state can be reviewed."><button type="button" className="btn-secondary" onClick={() => { actions.loadDemo(); setState({ settingsOpen: false }); }}>Load</button></Row>
                  </Group>
                )}
                <Group>
                  <Row title="Erase GNSIS from this computer" desc={eraseNote || "Deletes your key from this computer. This can’t be undone."}>
                    <button type="button" className="btn-danger" onClick={async () => {
                      const question = "Erase your GNSIS from this computer? This can’t be undone.";
                      const host = getHost();
                      if (!(host?.confirm ? await host.confirm(question, "Erase") : window.confirm(question))) return;
                      setEraseNote((await eraseIdentity()) ?? "");
                    }}>Erase…</button>
                  </Row>
                </Group>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Group({ title, note, children }: { title?: string; note?: string; children: ReactNode }) {
  return (
    <div>
      {title && <div className="group-title">{title}</div>}
      {note && <p className="muted small group-note">{note}</p>}
      <div className="card rows">{children}</div>
    </div>
  );
}
function Label({ title, desc }: { title: string; desc?: string }) {
  return <span className="row-label"><strong>{title}</strong>{desc && <span className="muted small">{desc}</span>}</span>;
}
function Row({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return <div className="row"><Label title={title} desc={desc} />{children}</div>;
}
function Select({ label, options, disabled }: { label: string; options: string[]; disabled?: boolean }) {
  return <select aria-label={label} className="select" disabled={disabled} title={disabled ? NOT_YET : undefined}>{options.map((o) => <option key={o}>{o}</option>)}</select>;
}

const NOT_YET = "Not in this build yet";
/** Said above a section whose greyed controls do nothing yet, so the grey is not left to be guessed at. */
const NOT_YET_NOTE = "The greyed-out settings here aren’t working in this build yet.";
