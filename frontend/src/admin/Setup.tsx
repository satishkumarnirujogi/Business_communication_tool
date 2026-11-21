import { Check, ChevronLeft, ChevronRight, Loader2, Stethoscope, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, Field, Input, Notice, Select } from "@/components/ui/form";
import { ApiError, post, setAuth } from "@/lib/api";
import { LLM_PRESETS, OpeningHoursEditor, type Hours } from "./shared";

interface Defaults {
  clinic: { name: string; phone: string; email: string; address: string; website: string; timezone: string;
            languages: string[]; currency: string; branding: { primary_color: string } };
  llm: { provider: string; model: string; base_url: string; tool_mode: string; api_key_set: boolean };
  data_source: { kind: string };
  opening_hours: Hours;
}

const STEPS = ["Clinic", "AI model", "Data", "Opening hours", "Admin account", "Logo & documents"];
const CATEGORIES = ["policy", "rules", "services", "prices", "offers", "doctors", "faq", "other"];

export default function SetupWizard({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [d, setD] = useState<Defaults | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [ds, setDs] = useState({ kind: "sqlite", database_url: "", excel_path: "", google_sheets_id: "", google_service_account_file: "" });
  const [admin, setAdmin] = useState({ name: "", email: "", password: "", confirm: "" });
  const [test, setTest] = useState<{ ok: boolean; error?: string; latency_ms?: number } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [restartMessage, setRestartMessage] = useState("");
  const [uploads, setUploads] = useState<string[]>([]);
  const [category, setCategory] = useState("faq");

  useEffect(() => {
    fetch("/api/setup/defaults").then((r) => r.json()).then((x: Defaults) => {
      setD(x);
      setDs((v) => ({ ...v, kind: x.data_source.kind }));
    }).catch(() => setError("Couldn't load the current settings."));
  }, []);

  if (!d) return <div className="grid h-full place-items-center text-sm text-muted-foreground">{error || "Loading…"}</div>;
  const set = (patch: Partial<Defaults>) => setD({ ...d, ...patch });
  const clinic = (patch: Partial<Defaults["clinic"]>) => set({ clinic: { ...d.clinic, ...patch } });
  const llm = (patch: Partial<Defaults["llm"]>) => { set({ llm: { ...d.llm, ...patch } }); setTest(null); };

  function validate(): string {
    if (step === 0 && (!d!.clinic.name.trim() || d!.clinic.phone.trim().length < 3)) return "Enter the clinic's name and phone number.";
    if (step === 0 && d!.clinic.languages.length === 0) return "Choose at least one language.";
    if (step === 1 && (!d!.llm.provider || !d!.llm.model)) return "Choose a provider and model.";
    if (step === 1 && !apiKey && !d!.llm.api_key_set && d!.llm.provider !== "ollama") return "Enter the API key.";
    if (step === 2 && ds.kind === "postgres" && !ds.database_url) return "Enter the Postgres connection string.";
    if (step === 2 && ds.kind === "google_sheets" && !ds.google_sheets_id) return "Enter the Google Sheet id.";
    if (step === 4) {
      if (!admin.name.trim() || !admin.email.includes("@")) return "Enter your name and email.";
      if (admin.password.length < 10) return "Use at least 10 characters for the password.";
      if (admin.password !== admin.confirm) return "The passwords don't match.";
    }
    return "";
  }

  async function testLlm() {
    setBusy(true);
    try {
      setTest(await post("/api/setup/test-llm", { ...d!.llm, api_key: apiKey || null }));
    } catch (e) {
      setTest({ ok: false, error: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    setError("");
    try {
      const r = await post<{ restart_required: boolean; token?: string; message?: string }>("/api/setup/complete", {
        clinic: d!.clinic, llm: { ...d!.llm, api_key: apiKey || null }, opening_hours: d!.opening_hours,
        data_source: Object.fromEntries(Object.entries(ds).filter(([, v]) => v)),
        admin: { name: admin.name, email: admin.email, password: admin.password },
      });
      if (r.restart_required) setRestartMessage(r.message ?? "Restart the server, then sign in.");
      else {
        setAuth({ kind: "session", token: r.token! });
        setStep(5);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't save the setup.");
    } finally {
      setBusy(false);
    }
  }

  async function upload(kind: "logo" | "doc", file: File) {
    const form = new FormData();
    form.append("file", file);
    if (kind === "doc") form.append("category", category);
    setBusy(true);
    try {
      await post(kind === "logo" ? "/api/admin/settings/logo" : "/api/admin/knowledge", form);
      setUploads((u) => [...u, kind === "logo" ? `Logo: ${file.name}` : `${file.name} (${category})`]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function next() {
    const problem = validate();
    if (problem) return setError(problem);
    setError("");
    if (step === 4) return finish();
    setStep(step + 1);
  }

  if (restartMessage) {
    return (
      <Center>
        <Notice tone="ok">Setup saved and your admin account was created.</Notice>
        <p className="text-sm">{restartMessage}</p>
      </Center>
    );
  }

  return (
    <Center wide>
      <div className="flex items-center gap-3">
        <span className="grid h-10 w-10 place-items-center rounded-lg bg-primary-soft text-primary"><Stethoscope className="h-5 w-5" /></span>
        <div>
          <h1 className="text-lg font-semibold">Set up your clinic assistant</h1>
          <p className="text-sm text-muted-foreground">About five minutes. You can change everything later in Settings.</p>
        </div>
      </div>
      <ol className="flex flex-wrap gap-1.5 text-xs">
        {STEPS.map((s, i) => (
          <li key={s} className={"flex items-center gap-1 rounded-full px-2.5 py-1 " +
            (i === step ? "bg-primary text-white" : i < step ? "bg-primary-soft text-primary" : "bg-muted text-muted-foreground")}>
            {i < step ? <Check className="h-3 w-3" /> : <span>{i + 1}</span>}{s}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Clinic name"><Input value={d.clinic.name} onChange={(e) => clinic({ name: e.target.value })} /></Field>
          <Field label="Phone"><Input value={d.clinic.phone} onChange={(e) => clinic({ phone: e.target.value })} /></Field>
          <Field label="Email"><Input type="email" value={d.clinic.email} onChange={(e) => clinic({ email: e.target.value })} /></Field>
          <Field label="Website"><Input value={d.clinic.website} onChange={(e) => clinic({ website: e.target.value })} /></Field>
          <Field label="Address" className="sm:col-span-2"><Input value={d.clinic.address} onChange={(e) => clinic({ address: e.target.value })} /></Field>
          <Field label="Timezone" hint="e.g. Europe/Berlin"><Input value={d.clinic.timezone} onChange={(e) => clinic({ timezone: e.target.value })} /></Field>
          <Field label="Currency"><Input value={d.clinic.currency} maxLength={8} onChange={(e) => clinic({ currency: e.target.value })} /></Field>
          <div className="grid gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Languages the assistant speaks</span>
            <div className="flex gap-4 text-sm">
              {[["de", "German"], ["en", "English"]].map(([code, name]) => (
                <label key={code} className="flex items-center gap-2">
                  <input type="checkbox" checked={d.clinic.languages.includes(code)}
                         onChange={(e) => clinic({ languages: e.target.checked ? [...d.clinic.languages, code] : d.clinic.languages.filter((x) => x !== code) })} />
                  {name}
                </label>
              ))}
            </div>
          </div>
          <Field label="Brand colour"><Input type="color" className="h-9 w-20 p-1" value={d.clinic.branding.primary_color || "#0f9d94"}
                                             onChange={(e) => clinic({ branding: { primary_color: e.target.value } })} /></Field>
        </div>
      )}

      {step === 1 && (
        <div className="grid gap-3">
          <Field label="Provider">
            <Select value={d.llm.provider} onChange={(e) => {
              const p = LLM_PRESETS.find((x) => x.provider === e.target.value);
              llm({ provider: e.target.value, model: p?.model ?? d.llm.model, base_url: p?.base_url ?? "" });
            }}>
              {!LLM_PRESETS.some((p) => p.provider === d.llm.provider) && d.llm.provider && <option value={d.llm.provider}>{d.llm.provider}</option>}
              {LLM_PRESETS.map((p) => <option key={p.provider} value={p.provider}>{p.label}</option>)}
            </Select>
          </Field>
          <Field label="Model" hint={LLM_PRESETS.find((p) => p.provider === d.llm.provider)?.hint}>
            <Input value={d.llm.model} onChange={(e) => llm({ model: e.target.value })} />
          </Field>
          <Field label="API key" hint={d.llm.api_key_set ? "A key is already saved; leave empty to keep it." : "From the provider's dashboard. Stored only in the server's .env file."}>
            <Input type="password" autoComplete="off" value={apiKey} onChange={(e) => { setApiKey(e.target.value); setTest(null); }} />
          </Field>
          {["ollama", "azure"].includes(d.llm.provider) && (
            <Field label="Server address"><Input value={d.llm.base_url} placeholder="http://localhost:11434" onChange={(e) => llm({ base_url: e.target.value })} /></Field>
          )}
          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={testLlm} disabled={busy}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}Test connection</Button>
            {test && (test.ok
              ? <span className="text-sm text-emerald-700">Connected ({test.latency_ms} ms)</span>
              : <span className="text-sm text-red-700">{test.error}</span>)}
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="grid gap-3">
          {[
            ["sqlite", "Built-in database", "No setup. Best for one server."],
            ["postgres", "Postgres / Supabase", "Use an existing Postgres database."],
            ["excel", "Excel file", "Staff edit doctors and hours in an .xlsx file."],
            ["google_sheets", "Google Sheets", "Shared editing in the browser (needs a service account)."],
          ].map(([kind, title, text]) => (
            <label key={kind} className={"flex cursor-pointer items-start gap-3 rounded-lg border p-3 " + (ds.kind === kind ? "border-primary bg-primary-soft" : "border-border")}>
              <input type="radio" name="ds" className="mt-1" checked={ds.kind === kind} onChange={() => setDs({ ...ds, kind })} />
              <span><span className="block text-sm font-medium">{title}</span><span className="text-xs text-muted-foreground">{text}</span></span>
            </label>
          ))}
          {ds.kind === "postgres" && <Field label="Connection string" hint="Supabase: Connect → Session pooler. URL-encode special characters in the password.">
            <Input type="password" value={ds.database_url} onChange={(e) => setDs({ ...ds, database_url: e.target.value })} /></Field>}
          {ds.kind === "excel" && <Field label="Path to the .xlsx file on the server" hint="Start from templates/clinic_template.xlsx. Leave empty for data/clinic.xlsx.">
            <Input value={ds.excel_path} onChange={(e) => setDs({ ...ds, excel_path: e.target.value })} /></Field>}
          {ds.kind === "google_sheets" && <>
            <Field label="Google Sheet id" hint="From the URL: docs.google.com/spreadsheets/d/<id>/edit"><Input value={ds.google_sheets_id} onChange={(e) => setDs({ ...ds, google_sheets_id: e.target.value })} /></Field>
            <Field label="Service account key file on the server" hint="See docs/google-sheets.md"><Input value={ds.google_service_account_file} placeholder="config/google-service-account.json" onChange={(e) => setDs({ ...ds, google_service_account_file: e.target.value })} /></Field>
          </>}
          {ds.kind !== d.data_source.kind && <Notice tone="warn">Changing the data source needs a server restart after setup.</Notice>}
        </div>
      )}

      {step === 3 && <OpeningHoursEditor value={d.opening_hours} onChange={(h) => set({ opening_hours: h })} />}

      {step === 4 && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Your name"><Input value={admin.name} onChange={(e) => setAdmin({ ...admin, name: e.target.value })} /></Field>
          <Field label="Email (used to sign in)"><Input type="email" autoComplete="username" value={admin.email} onChange={(e) => setAdmin({ ...admin, email: e.target.value })} /></Field>
          <Field label="Password" hint="At least 10 characters, mixing letters with numbers or symbols."><Input type="password" autoComplete="new-password" value={admin.password} onChange={(e) => setAdmin({ ...admin, password: e.target.value })} /></Field>
          <Field label="Repeat password"><Input type="password" autoComplete="new-password" value={admin.confirm} onChange={(e) => setAdmin({ ...admin, confirm: e.target.value })} /></Field>
        </div>
      )}

      {step === 5 && (
        <div className="grid gap-4">
          <Notice tone="ok">Your clinic is set up and you're signed in. These last two steps are optional.</Notice>
          <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-dashed border-border p-4 text-sm hover:border-primary">
            <Upload className="h-4 w-4 text-primary" /> Upload your logo (PNG, JPG, WebP or SVG, up to 1 MB)
            <input type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml" className="hidden"
                   onChange={(e) => e.target.files?.[0] && upload("logo", e.target.files[0])} />
          </label>
          <div className="grid gap-2 rounded-lg border border-dashed border-border p-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Upload className="h-4 w-4 text-primary" /> Add documents the assistant should answer from:
              <Select className="w-36" value={category} onChange={(e) => setCategory(e.target.value)}>
                {CATEGORIES.map((c) => <option key={c}>{c}</option>)}
              </Select>
              <label className="cursor-pointer rounded-lg border border-border px-3 py-1.5 hover:bg-muted">
                Choose file…
                <input type="file" accept=".pdf,.docx,.txt,.md,.csv" className="hidden" onChange={(e) => e.target.files?.[0] && upload("doc", e.target.files[0])} />
              </label>
            </div>
            <span className="text-xs text-muted-foreground">Policies, price lists, offers, doctor profiles, FAQ. PDF, DOCX, TXT, MD or CSV.</span>
          </div>
          {uploads.length > 0 && <ul className="list-disc pl-5 text-sm">{uploads.map((u) => <li key={u}>{u}</li>)}</ul>}
        </div>
      )}

      {error && <Notice tone="error" onClose={() => setError("")}>{error}</Notice>}
      <div className="flex justify-between">
        {step > 0 && step < 5 ? <Button variant="outline" onClick={() => { setError(""); setStep(step - 1); }}><ChevronLeft className="h-4 w-4" />Back</Button> : <span />}
        {step < 5
          ? <Button onClick={next} disabled={busy}>{busy && <Loader2 className="h-4 w-4 animate-spin" />}{step === 4 ? "Create account & finish" : "Next"}<ChevronRight className="h-4 w-4" /></Button>
          : <Button onClick={onDone} disabled={busy}>Go to the dashboard<ChevronRight className="h-4 w-4" /></Button>}
      </div>
    </Center>
  );
}

function Center({ children, wide }: { children: React.ReactNode; wide?: boolean }) {
  return (
    <div className="min-h-full overflow-auto p-4 sm:p-8">
      <Card className={"mx-auto grid gap-5 p-6 " + (wide ? "max-w-2xl" : "max-w-md")}>{children}</Card>
    </div>
  );
}
