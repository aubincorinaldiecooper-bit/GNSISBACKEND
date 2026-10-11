import { useState } from "react";
import { Face, GNSIS_ICON_NAME } from "../lib/face";
import { copyText } from "../lib/platform";
import { createIdentity, enterDesktop, useStore } from "../store/store";
import * as I from "./Icons";

/** Account setup: no email, no password. The computer becomes the identity. */
export function Setup() {
  const s = useStore((x) => x);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  const create = async () => {
    setError("");
    const problem = await createIdentity();
    if (problem) setError(problem);
  };

  if (s.phase === "ready" && s.identity) {
    const id = s.identity;
    return (
      <div data-hit className="setup-card glass-card">
        <div className="setup-face ready">
          <Face name={id.publicId} gnsis size={180} joyful pop />
        </div>
        <h1>Your GNSIS is ready</h1>
        <p className="lead">We created your private identity and unique character.</p>
        <div className="setup-id">
          <label htmlFor="gnsis-id">GNSIS ID</label>
          <div className="id-field">
            <input id="gnsis-id" readOnly value={id.publicId} />
            <button type="button" aria-label={copied ? "Copied" : "Copy GNSIS ID"} onClick={async () => {
              if (await copyText(id.publicId)) { setCopied(true); setTimeout(() => setCopied(false), 1800); }
            }}>{copied ? <I.Check size={24} /> : <I.Copy size={24} />}</button>
          </div>
          <span className="note" aria-live="polite">{copied ? "Copied to your clipboard." : "This ID is derived from your device."}</span>
        </div>
        <p className="note center">Your private key stays on this device.</p>
        <button type="button" className="big-btn" onClick={() => enterDesktop(id, s.demo)}>Continue</button>
      </div>
    );
  }

  return (
    <div data-hit className="setup-card glass-card">
      <div className="setup-face"><Face name={GNSIS_ICON_NAME} gnsis size={150} working={s.creating} /></div>
      <h1>Welcome to GNSIS</h1>
      <p className="lead">This computer becomes your identity.</p>
      <p className="note center spaced">No email. No password.</p>
      <button type="button" className="big-btn" aria-busy={s.creating} disabled={s.creating} onClick={create}>{s.creating ? "Creating your GNSIS…" : "Create my GNSIS"}</button>
      <p className="note center" aria-live="polite">{error || (s.creating ? "Making a private key on this device." : "A private key is created and stays on this device.")}</p>
    </div>
  );
}
