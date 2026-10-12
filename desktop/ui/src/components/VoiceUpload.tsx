import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import type { VoicePreferences } from "../../../src/renderer/bridge";
import * as I from "./Icons";

/** What the server accepts for a reference voice. */
export const MAX_VOICE_BYTES = 5 * 1024 * 1024;
export const MIN_VOICE_S = 3;
export const MAX_VOICE_S = 30;
const BARS = 56;

export interface Clip {
  fileName: string;
  url: string;
  duration: number;
  peaks: number[];
  audioB64: string;
}

interface Upload {
  id: number;
  name: string;
  transcript: string;
  clip: Clip;
  state: "uploading" | "ready" | "failed";
  error?: string;
}

/** Loudest sample in each of `bars` equal slices, scaled so the loudest slice is 1. */
export function peaksOf(channel: Float32Array, bars = BARS): number[] {
  const size = Math.max(1, Math.floor(channel.length / bars));
  const out: number[] = [];
  for (let b = 0; b < bars; b++) {
    let max = 0;
    for (let i = b * size, end = Math.min(channel.length, i + size); i < end; i++) {
      const v = Math.abs(channel[i]!);
      if (v > max) max = v;
    }
    out.push(max);
  }
  const top = Math.max(...out, 1e-6);
  return out.map((v) => v / top);
}

/** Why a picked file can't be a reference voice, or null when it can. */
export function clipProblem(bytes: number, duration: number | null): string | null {
  if (bytes > MAX_VOICE_BYTES) return "This file is larger than 5 MB.";
  if (duration === null) return "This file isn’t a WAV recording GNSIS can read.";
  if (duration < MIN_VOICE_S || duration > MAX_VOICE_S) {
    return `This recording is ${Math.round(duration)} seconds. It needs to be 3 to 30 seconds.`;
  }
  return null;
}

/** The server's reason, without Electron's "Error invoking remote method" wrapping. */
export function uploadReason(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const text = raw.replace(/^Error invoking remote method '[^']*': (Error: )?/, "").trim();
  if (!text) return "The voice could not be uploaded.";
  const sentence = text[0]!.toUpperCase() + text.slice(1);
  return /[.!?]$/.test(sentence) ? sentence : sentence + ".";
}

function base64Of(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function readClip(file: File): Promise<Clip | string> {
  if (file.size > MAX_VOICE_BYTES) return clipProblem(file.size, 0)!;
  const buffer = await file.arrayBuffer();
  let duration: number | null = null;
  let peaks: number[] = [];
  const ctx = new AudioContext();
  try {
    const audio = await ctx.decodeAudioData(buffer.slice(0));
    duration = audio.duration;
    peaks = peaksOf(audio.getChannelData(0));
  } catch {
    duration = null;
  } finally {
    void ctx.close();
  }
  const problem = clipProblem(file.size, duration);
  if (problem) return problem;
  return { fileName: file.name, url: URL.createObjectURL(file), duration: duration!, peaks, audioB64: base64Of(buffer) };
}

const time = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

/** A recording as a row: play, its waveform across the row, its length, and whatever goes at the end. */
function ClipRow({ clip, fill, end, tone }: { clip: Clip; fill?: number; end?: ReactNode; tone?: "failed" }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [played, setPlayed] = useState(0);
  useEffect(() => {
    audio.current?.pause();
    audio.current = null;
    setPlaying(false);
    setPlayed(0);
    return () => audio.current?.pause();
  }, [clip.url]);
  const toggle = () => {
    if (!audio.current) {
      const a = new Audio(clip.url);
      a.ontimeupdate = () => setPlayed(a.duration ? a.currentTime / a.duration : 0);
      a.onended = () => { setPlaying(false); setPlayed(0); };
      audio.current = a;
    }
    const a = audio.current;
    if (a.paused) { void a.play(); setPlaying(true); } else { a.pause(); setPlaying(false); }
  };
  const progress = fill ?? played;
  return (
    <div className={"clip-row" + (tone ? " is-" + tone : "")}>
      {fill === undefined ? (
        <button type="button" className="clip-play" aria-label={playing ? "Pause" : "Play"} onClick={toggle}>
          {playing ? <I.Pause size={16} /> : <I.Play size={16} />}
        </button>
      ) : (
        <span className="clip-play" aria-hidden="true"><I.Play size={16} /></span>
      )}
      <div className="clip-wave" aria-hidden="true">
        {clip.peaks.map((p, i) => (
          <i key={i} className={(i + 0.5) / clip.peaks.length <= progress ? "on" : ""} style={{ height: `${Math.max(14, Math.round(p * 100))}%` }} />
        ))}
      </div>
      <span className="clip-time">{time(clip.duration)}</span>
      {end}
    </div>
  );
}

/**
 * Upload voice: one row, like the others, with the limits under its name. A
 * picked or dropped WAV opens a short form under it (play it back, name it,
 * type its words); uploading puts it in the list below right away, marked
 * Uploading…, then ticked, or red with the reason and a retry.
 */
export function VoiceUpload({ onUploaded }: { onUploaded: (prefs: VoicePreferences) => void }) {
  const picker = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState<Clip | null>(null);
  const [name, setName] = useState("");
  const [words, setWords] = useState("");
  const [problem, setProblem] = useState("");
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [fill, setFill] = useState(0);
  const seq = useRef(0);
  const ownedUrls = useRef(new Set<string>());
  const busy = uploads.some((u) => u.state === "uploading");

  useEffect(() => () => {
    for (const url of ownedUrls.current) URL.revokeObjectURL(url);
    ownedUrls.current.clear();
  }, []);

  useEffect(() => {
    if (!busy) return;
    setFill(0);
    const t = window.setInterval(() => setFill((f) => f + (0.92 - f) * 0.08), 120);
    return () => window.clearInterval(t);
  }, [busy]);

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setProblem("");
    const clip = await readClip(file);
    if (typeof clip === "string") { setProblem(clip); return; }
    ownedUrls.current.add(clip.url);
    setDraft((previous) => {
      if (previous) {
        URL.revokeObjectURL(previous.url);
        ownedUrls.current.delete(previous.url);
      }
      return clip;
    });
    setName(file.name.replace(/\.wav$/i, ""));
    setWords("");
  };
  const send = async (u: Upload) => {
    setUploads((list) => list.map((x) => (x.id === u.id ? { ...u, state: "uploading", error: undefined } : x)));
    try {
      const prefs = await window.gnsis.uploadVoiceProfile!(u.name, u.transcript, u.clip.audioB64);
      setUploads((list) => list.map((x) => (x.id === u.id ? { ...x, state: "ready" } : x)));
      if (prefs) onUploaded(prefs);
    } catch (error) {
      setUploads((list) => list.map((x) => (x.id === u.id ? { ...x, state: "failed", error: uploadReason(error) } : x)));
    }
  };
  const upload = () => {
    if (!draft || !name.trim() || !words.trim()) return;
    seq.current += 1;
    const u: Upload = { id: seq.current, name: name.trim(), transcript: words.trim(), clip: draft, state: "uploading" };
    setUploads((list) => [...list, u]);
    setDraft(null);
    void send(u);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    void pick(e.dataTransfer.files[0]);
  };
  const clearDraft = () => {
    setDraft((previous) => {
      if (previous) {
        URL.revokeObjectURL(previous.url);
        ownedUrls.current.delete(previous.url);
      }
      return null;
    });
  };

  return (
    <>
      <div className="row" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
        <span className="row-label"><strong>Upload voice</strong><span className="muted small">WAV, 3 to 30 seconds, up to 5 MB</span></span>
        <input ref={picker} type="file" accept=".wav,audio/wav,audio/x-wav" hidden onChange={(e) => { void pick(e.target.files?.[0]); e.target.value = ""; }} />
        <button type="button" className="select pick-btn" onClick={() => picker.current?.click()}>Choose WAV file…</button>
      </div>
      {problem && <div className="voice-error small" role="alert">{problem}</div>}
      {draft && (
        <div className="voice-draft">
          <ClipRow
            clip={draft}
            end={<button type="button" className="clip-end" aria-label="Remove file" onClick={clearDraft}><I.Close size={16} /></button>}
          />
          <input className="text-field" aria-label="Voice name" placeholder="Voice name" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
          <textarea className="persona-field words-field" aria-label="Words spoken in the recording" placeholder="The exact words spoken in the recording" rows={2} value={words} onChange={(e) => setWords(e.target.value)} />
          <div className="field-actions">
            <button type="button" className="btn-secondary compact" onClick={clearDraft}>Cancel</button>
            <button type="button" className="btn-secondary compact" disabled={!name.trim() || !words.trim()} onClick={upload}>Upload</button>
          </div>
        </div>
      )}
      {uploads.map((u) => (
        <div key={u.id} className="voice-item">
          <strong>{u.name}</strong>
          <ClipRow
            clip={u.clip}
            tone={u.state === "failed" ? "failed" : undefined}
            fill={u.state === "uploading" ? fill : undefined}
            end={
              u.state === "ready" ? <span className="clip-end is-ok" aria-label="Uploaded"><I.Check size={18} /></span>
              : u.state === "failed" ? <button type="button" className="clip-end" aria-label="Try again" onClick={() => void send(u)}><I.Retry size={16} /></button>
              : null
            }
          />
          {u.state === "uploading" && <span className="muted small" role="status">Uploading…</span>}
          {u.state === "failed" && <span className="voice-error-text small" role="alert">{u.error}</span>}
        </div>
      ))}
    </>
  );
}
