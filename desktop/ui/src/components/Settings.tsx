import { useState, type ReactNode } from "react";
import { Face } from "../lib/face";
import { shareLink } from "../lib/identity";
import { copyText } from "../lib/platform";
import { actions, eraseIdentity, getIdentityStore, setState, useStore } from "../store/store";
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
  const [section, setSection] = useState<Section>("profile");
  // These switches are the product's intended settings; nothing reads them yet.
  const [tg, setTg] = useState({ findable: true, autosend: true, speak: false, approve: true, browser: true, nFinish: true, nNeeds: true, nSound: false, motion: false });
  const [copied, setCopied] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [dock, setDock] = useState<"bottom" | "top">("bottom");
  const [glass, setGlass] = useState(55);
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
    <button type="button" role="switch" aria-label={label} aria-checked={tg[k]} className={"switch" + (tg[k] ? " on" : "")} onClick={() => flip(k)}><span /></button>
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
                <Group title="Sharing">
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
              <Group>
                <Row title="Wake gesture" desc="Starts or ends live voice with whoever is in front, same as the voice button. There is no hardware gesture yet; ⌥Space stands in while GNSIS is focused."><span className="chip">⌥Space</span></Row>
                <Row title="Microphone"><Select label="Microphone" options={["Built-in microphone", "External microphone"]} /></Row>
                {caps.transcript && <Row title="Send when I stop talking" desc="For dictation. Sends after a short pause.">{toggle("autosend", "Send when I stop talking")}</Row>}
                <Row title="Read replies out loud">{toggle("speak", "Read replies out loud")}</Row>
                <Row title="Voice"><Select label="Voice" options={["Calm", "Bright", "Deep"]} /></Row>
              </Group>
            )}
            {section === "agents" && (
              <Group>
                <Row title="Ask before sending emails or messages" desc="Agents show you an approval card first.">{toggle("approve", "Ask before sending emails or messages")}</Row>
                <Row title="Let agents use a browser" desc="Needed for tasks like finding a recipe or filling out a form.">{toggle("browser", "Let agents use a browser")}</Row>
                <Row title="Remove finished agents from the dock" desc="They stay in All agents."><Select label="Remove finished agents from the dock" options={["After 10 minutes", "After 1 hour", "After 1 day", "Never"]} /></Row>
              </Group>
            )}
            {section === "notif" && (
              <Group>
                <Row title="When a background agent finishes">{toggle("nFinish", "When a background agent finishes")}</Row>
                <Row title="When an agent needs you">{toggle("nNeeds", "When an agent needs you")}</Row>
                <Row title="Play a sound">{toggle("nSound", "Play a sound")}</Row>
              </Group>
            )}
            {section === "appear" && (
              <Group>
                <Row title="Glass transparency"><input type="range" aria-label="Glass transparency" min={20} max={85} value={glass} onChange={(e) => setGlass(Number(e.target.value))} /></Row>
                <Row title="Dock position">
                  <div className="segmented" role="radiogroup" aria-label="Dock position">
                    {(["bottom", "top"] as const).map((d) => <button key={d} type="button" role="radio" aria-checked={dock === d} className={dock === d ? "on" : ""} onClick={() => setDock(d)}>{d === "bottom" ? "Bottom" : "Top"}</button>)}
                  </div>
                </Row>
                <Row title="Reduce motion" desc="Turns off breathing faces and sliding windows.">{toggle("motion", "Reduce motion")}</Row>
              </Group>
            )}
            {section === "privacy" && (
              <>
                <Group>
                  <Row title="Private key" desc={keyNote}><span className="chip green">{identity.storage === "keychain" ? "In this Mac’s Keychain" : "On this device"}</span></Row>
                  <Row title="Back up your key" desc="If this computer is lost, a backup is the only way to get your GNSIS back."><button type="button" className="btn-secondary" disabled title="Not in this build yet">Back up…</button></Row>
                  <Row title="Conversation history" desc="Stored on this computer."><button type="button" className="btn-secondary" disabled title="Not in this build yet">Clear history…</button></Row>
                </Group>
                <Group title="Developer">
                  <Row title="Load demo agents" desc="Fills the dock with sample agents so every state can be reviewed."><button type="button" className="btn-secondary" onClick={() => { actions.loadDemo(); setState({ settingsOpen: false }); }}>Load</button></Row>
                </Group>
                <Group>
                  <Row title="Erase GNSIS from this computer" desc="Deletes your key, agents and history here. This can’t be undone.">
                    <button type="button" className="btn-danger" onClick={async () => {
                      if (!window.confirm("Erase your GNSIS from this computer? This can’t be undone.")) return;
                      await eraseIdentity();
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

function Group({ title, children }: { title?: string; children: ReactNode }) {
  return <div>{title && <div className="group-title">{title}</div>}<div className="card rows">{children}</div></div>;
}
function Label({ title, desc }: { title: string; desc?: string }) {
  return <span className="row-label"><strong>{title}</strong>{desc && <span className="muted small">{desc}</span>}</span>;
}
function Row({ title, desc, children }: { title: string; desc?: string; children: ReactNode }) {
  return <div className="row"><Label title={title} desc={desc} />{children}</div>;
}
function Select({ label, options }: { label: string; options: string[] }) {
  return <select aria-label={label} className="select">{options.map((o) => <option key={o}>{o}</option>)}</select>;
}
