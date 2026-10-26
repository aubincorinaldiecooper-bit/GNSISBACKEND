import { useEffect, useState } from "react";

type Model = { id: string; name: string };

/** Popular coding models on OpenRouter, shown before any search. */
export const POPULAR_CODING_MODELS: Model[] = [
  { id: "deepseek/deepseek-v4-flash", name: "DeepSeek V4 Flash" },
  { id: "deepseek/deepseek-v4-pro", name: "DeepSeek V4 Pro" },
  { id: "anthropic/claude-sonnet-5.5", name: "Claude Sonnet 5.5" },
  { id: "anthropic/claude-opus-5.5", name: "Claude Opus 5.5" },
  { id: "openai/gpt-5.5", name: "GPT-5.5" },
  { id: "google/gemini-3.8-flash", name: "Gemini 3.8 Flash" },
  { id: "moonshotai/kimi-k2.7-code", name: "Kimi K2.7 Code" },
  { id: "z-ai/glm-5.3", name: "GLM 5.3" },
  { id: "minimax/minimax-m3", name: "MiniMax M3" },
];
const DEFAULT_MODEL = POPULAR_CODING_MODELS[0].id;

export function WorkerModelPicker({ value, disabled, onPick }: {
  value: string;
  disabled?: boolean;
  onPick: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [all, setAll] = useState<Model[] | null>(null);
  const [note, setNote] = useState("");
  const current = value || DEFAULT_MODEL;
  const q = query.trim().toLowerCase();
  useEffect(() => {
    if (!q || all || !window.gnsis.workerModels) return;
    void window.gnsis.workerModels().then(setAll).catch(() => setNote("Couldn’t reach OpenRouter to search."));
  }, [q, all]);
  const shown = q
    ? (all ?? []).filter((m) => {
        const text = `${m.id} ${m.name}`.toLowerCase();
        return q.split(/\s+/).every((word) => text.includes(word));
      }).slice(0, 8)
    : POPULAR_CODING_MODELS;
  return (
    <div className="field-block model-picker">
      <input
        className="text-field"
        type="search"
        aria-label="Search OpenRouter models"
        placeholder="Search all OpenRouter models"
        value={query}
        disabled={disabled}
        onChange={(e) => { setQuery(e.target.value); setNote(""); }}
      />
      <div className="model-list" role="listbox" aria-label="Model for background jobs">
        {shown.map((m) => (
          <button
            key={m.id}
            type="button"
            role="option"
            aria-selected={m.id === current}
            className={"model-option" + (m.id === current ? " on" : "")}
            disabled={disabled}
            onClick={() => onPick(m.id)}
          >
            <span>{m.name.replace(/^[^:]+:\s*/, "")}</span>
            <span className="muted small mono">{m.id}</span>
          </button>
        ))}
        {q && all && shown.length === 0 && <div className="muted small">No OpenRouter model matches “{query.trim()}”.</div>}
        {q && !all && !note && <div className="muted small">Searching…</div>}
      </div>
      {note && <div className="muted small">{note}</div>}
    </div>
  );
}
