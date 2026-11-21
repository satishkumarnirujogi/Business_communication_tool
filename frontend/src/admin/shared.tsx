import { DateTime } from "luxon";
import { Input } from "@/components/ui/form";

export type Hours = Record<string, string[] | null>;
export const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

export const LLM_PRESETS = [
  { provider: "openrouter", label: "OpenRouter (many models, one key)", model: "anthropic/claude-haiku-5.5", hint: "Any model id from openrouter.ai/models", base_url: "" },
  { provider: "openai", label: "OpenAI", model: "gpt-4o-mini", hint: "e.g. gpt-4o-mini", base_url: "" },
  { provider: "anthropic", label: "Anthropic", model: "claude-haiku-5-5", hint: "e.g. claude-haiku-5-5", base_url: "" },
  { provider: "mistral", label: "Mistral", model: "mistral-small-latest", hint: "e.g. mistral-small-latest", base_url: "" },
  { provider: "groq", label: "Groq", model: "llama-3.3-70b-versatile", hint: "e.g. llama-3.3-70b-versatile", base_url: "" },
  { provider: "azure", label: "Azure OpenAI", model: "", hint: "Your deployment name", base_url: "https://YOUR-RESOURCE.openai.azure.com" },
  { provider: "ollama", label: "Ollama (runs locally, no key)", model: "qwen3", hint: "A model you pulled with `ollama pull`", base_url: "http://localhost:11434" },
];

export function OpeningHoursEditor({ value, onChange }: { value: Hours; onChange: (h: Hours) => void }) {
  return (
    <div className="grid gap-2">
      {DAYS.map((day) => {
        const h = value[day];
        return (
          <div key={day} className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex w-36 items-center gap-2">
              <input type="checkbox" checked={!!h} onChange={(e) => onChange({ ...value, [day]: e.target.checked ? ["09:00", "17:00"] : null })} />
              <span className="capitalize">{day}</span>
            </label>
            {h ? (
              <>
                <Input type="time" step={900} className="w-32" aria-label={`${day} opens`} value={h[0]} onChange={(e) => onChange({ ...value, [day]: [e.target.value, h[1]] })} />
                <span className="text-muted-foreground">to</span>
                <Input type="time" step={900} className="w-32" aria-label={`${day} closes`} value={h[1]} onChange={(e) => onChange({ ...value, [day]: [h[0], e.target.value] })} />
              </>
            ) : <span className="text-muted-foreground">Closed</span>}
          </div>
        );
      })}
    </div>
  );
}

export const when = (iso: string | null | undefined, fmt = "d LLL yyyy, HH:mm") =>
  iso ? DateTime.fromISO(iso).toFormat(fmt) : "—";

export const ago = (iso: string) => DateTime.fromISO(iso).toRelative() ?? "";
