import { Loader2, Plus, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Field, Input, Notice, PageHeader, Select, Textarea, td, th } from "@/components/ui/form";
import { api, patch, post, put, type Me } from "@/lib/api";
import { LLM_PRESETS, OpeningHoursEditor, type Hours, when } from "../shared";
import { attempt, useApi } from "../useApi";
import ChannelsTab from "./ChannelsTab";

const TABS = [
  ["llm", "AI model"], ["clinic", "Clinic & branding"], ["rules", "Rules"], ["users", "Users"], ["channels", "Channels"], ["data", "Data source"], ["account", "My password"],
] as const;
type Tab = (typeof TABS)[number][0];

interface AllSettings {
  llm: { provider: string; model: string; base_url: string; tool_mode: string; api_key_set: boolean };
  clinic: { name: string; phone: string; email: string; address: string; website: string; timezone: string; languages: string[];
            currency: string; branding: { logo_url: string; primary_color: string } };
  data_source: { kind: string; sqlite_path: string; excel_path: string; google_sheets_id: string; database_url_set: boolean };
  knowledge: { embedding_provider: string; embedding_model: string; vector_store: string };
}

export default function SettingsPage({ me, onAuthError, onClinicChange }: { me: Me; onAuthError: () => void; onClinicChange: () => void }) {
  const [tab, setTab] = useState<Tab>("llm");
  const all = useApi(() => api<AllSettings>("/api/admin/settings"), [], onAuthError);
  const [ok, setOk] = useState("");
  const [error, setError] = useState("");
  const run = async <T,>(fn: () => Promise<T>, message: string) => {
    setOk("");
    setError("");
    const r = await attempt(fn, setError, onAuthError);
    if (r !== undefined) { setOk(message); all.refresh(); }
    return r;
  };

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader title="Settings" />
      <div className="mb-4 flex flex-wrap gap-1 border-b border-border">
        {TABS.map(([id, label]) => (
          <button key={id} onClick={() => { setTab(id); setOk(""); setError(""); }}
                  className={"-mb-px border-b-2 px-3 py-2 text-sm font-medium " + (tab === id ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground")}>
            {label}
          </button>
        ))}
      </div>
      {ok && <div className="mb-3"><Notice tone="ok" onClose={() => setOk("")}>{ok}</Notice></div>}
      {(error || all.error) && <div className="mb-3"><Notice tone="error" onClose={() => setError("")}>{error || all.error}</Notice></div>}
      {all.data && tab === "llm" && <LlmTab s={all.data.llm} run={run} onAuthError={onAuthError} setError={setError} />}
      {all.data && tab === "clinic" && <ClinicTab s={all.data.clinic} run={run} onSaved={onClinicChange} />}
      {tab === "rules" && <RulesTab run={run} onAuthError={onAuthError} />}
      {tab === "users" && <UsersTab me={me} run={run} onAuthError={onAuthError} />}
      {tab === "channels" && <ChannelsTab run={run} onAuthError={onAuthError} />}
      {all.data && tab === "data" && <DataTab s={all.data} run={run} />}
      {tab === "account" && <AccountTab me={me} run={run} />}
    </div>
  );
}

type Run = <T>(fn: () => Promise<T>, message: string) => Promise<T | undefined>;

function LlmTab({ s, run, onAuthError, setError }: { s: AllSettings["llm"]; run: Run; onAuthError: () => void; setError: (m: string) => void }) {
  const [v, setV] = useState({ ...s, api_key: "" });
  const [test, setTest] = useState<{ ok: boolean; error?: string; latency_ms?: number; reply?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const preset = LLM_PRESETS.find((p) => p.provider === v.provider);
  const body = { provider: v.provider, model: v.model, api_key: v.api_key || null, base_url: v.base_url || null, tool_mode: v.tool_mode };

  return (
    <Card className="grid gap-3 p-5">
      <Field label="Provider">
        <Select value={v.provider} onChange={(e) => { const p = LLM_PRESETS.find((x) => x.provider === e.target.value); setV({ ...v, provider: e.target.value, model: p?.model ?? v.model, base_url: p?.base_url ?? "" }); setTest(null); }}>
          {!preset && <option value={v.provider}>{v.provider || "Choose…"}</option>}
          {LLM_PRESETS.map((p) => <option key={p.provider} value={p.provider}>{p.label}</option>)}
        </Select>
      </Field>
      <Field label="Model" hint={preset?.hint}><Input value={v.model} onChange={(e) => { setV({ ...v, model: e.target.value }); setTest(null); }} /></Field>
      <Field label="API key" hint={s.api_key_set ? "A key is saved. Leave empty to keep it; it is never shown again." : "Not set yet."}>
        <Input type="password" autoComplete="off" value={v.api_key} onChange={(e) => { setV({ ...v, api_key: e.target.value }); setTest(null); }} />
      </Field>
      <Field label="Server address (Ollama, Azure, self-hosted)"><Input value={v.base_url} onChange={(e) => setV({ ...v, base_url: e.target.value })} /></Field>
      <Field label="Tool calling" hint="Auto works for almost every model; JSON helps small local models.">
        <Select value={v.tool_mode} onChange={(e) => setV({ ...v, tool_mode: e.target.value })}>
          <option value="auto">Auto (recommended)</option><option value="native">Native only</option><option value="json">JSON mode</option>
        </Select>
      </Field>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" disabled={busy} onClick={async () => {
          setBusy(true);
          setTest((await attempt(() => post("/api/admin/settings/llm/test", body), setError, onAuthError)) ?? null);
          setBusy(false);
        }}>{busy && <Loader2 className="h-4 w-4 animate-spin" />}Test connection</Button>
        <Button onClick={() => run(() => put("/api/admin/settings/llm", body), "AI model saved. New chats use it right away.")}>Save</Button>
        {test && (test.ok ? <span className="text-sm text-emerald-700">Connected in {test.latency_ms} ms</span> : <span className="text-sm text-red-700">{test.error}</span>)}
      </div>
    </Card>
  );
}

function ClinicTab({ s, run, onSaved }: { s: AllSettings["clinic"]; run: Run; onSaved: () => void }) {
  const [v, setV] = useState(s);
  const save = async () => { if (await run(() => put("/api/admin/settings/clinic", v), "Clinic details saved.")) onSaved(); };
  async function logo(file: File) {
    const form = new FormData();
    form.append("file", file);
    const r = await run(() => post<{ logo_url: string }>("/api/admin/settings/logo", form), "Logo uploaded.");
    if (r) { setV({ ...v, branding: { ...v.branding, logo_url: r.logo_url } }); onSaved(); }
  }
  return (
    <Card className="grid gap-3 p-5 sm:grid-cols-2">
      <Field label="Clinic name"><Input value={v.name} onChange={(e) => setV({ ...v, name: e.target.value })} /></Field>
      <Field label="Phone"><Input value={v.phone} onChange={(e) => setV({ ...v, phone: e.target.value })} /></Field>
      <Field label="Email"><Input value={v.email} onChange={(e) => setV({ ...v, email: e.target.value })} /></Field>
      <Field label="Website"><Input value={v.website} onChange={(e) => setV({ ...v, website: e.target.value })} /></Field>
      <Field label="Address" className="sm:col-span-2"><Input value={v.address} onChange={(e) => setV({ ...v, address: e.target.value })} /></Field>
      <Field label="Timezone" hint="e.g. Europe/Berlin"><Input value={v.timezone} onChange={(e) => setV({ ...v, timezone: e.target.value })} /></Field>
      <Field label="Currency"><Input value={v.currency} onChange={(e) => setV({ ...v, currency: e.target.value })} /></Field>
      <div className="grid gap-1.5">
        <span className="text-xs font-medium text-muted-foreground">Languages</span>
        <div className="flex gap-4 text-sm">{[["de", "German"], ["en", "English"]].map(([c, n]) => (
          <label key={c} className="flex items-center gap-2"><input type="checkbox" checked={v.languages.includes(c)}
            onChange={(e) => setV({ ...v, languages: e.target.checked ? [...v.languages, c] : v.languages.filter((x) => x !== c) })} />{n}</label>
        ))}</div>
      </div>
      <Field label="Brand colour"><Input type="color" className="w-20 p-1" value={v.branding.primary_color || "#0f9d94"} onChange={(e) => setV({ ...v, branding: { ...v.branding, primary_color: e.target.value } })} /></Field>
      <div className="flex items-center gap-3 sm:col-span-2">
        {v.branding.logo_url && <img src={v.branding.logo_url} alt="Logo" className="h-12 w-12 rounded-lg border border-border object-cover" />}
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-muted">
          <Upload className="h-4 w-4" />Upload logo
          <input type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml" className="hidden" onChange={(e) => e.target.files?.[0] && logo(e.target.files[0])} />
        </label>
      </div>
      <div className="sm:col-span-2"><Button onClick={save}>Save</Button></div>
    </Card>
  );
}

interface RulesForm {
  opening_hours: Hours; closed_dates: string[]; booking: Record<string, number>;
  cancellation: { min_notice_hours: number; policy_text: string }; reschedule: { min_notice_hours: number };
  escalation_keywords: Record<string, string[]>;
}
const BOOKING_LABELS: Record<string, string> = {
  slot_minutes: "Calendar grid (minutes)", default_duration_minutes: "Default visit length (minutes)",
  min_lead_time_hours: "Earliest booking (hours ahead)", max_days_ahead: "Latest booking (days ahead)",
  max_search_span_days: "Longest search (days)", max_active_appointments_per_patient: "Upcoming appointments per patient",
};

function RulesTab({ run, onAuthError }: { run: Run; onAuthError: () => void }) {
  const { data, error } = useApi(() => api<RulesForm>("/api/admin/settings/rules"), [], onAuthError);
  const [v, setV] = useState<RulesForm | null>(null);
  useEffect(() => { if (data) setV(data); }, [data]);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!v) return null;
  const save = () => run(() => put<{ slots: { added: number; removed: number; conflicts: string[] } }>("/api/admin/settings/rules", v),
    "Rules saved. Bookable times were updated.");
  return (
    <div className="grid gap-4">
      <Card className="p-5"><h3 className="mb-3 text-sm font-semibold">Opening hours</h3>
        <OpeningHoursEditor value={v.opening_hours} onChange={(h) => setV({ ...v, opening_hours: h })} /></Card>
      <Card className="grid gap-3 p-5"><h3 className="text-sm font-semibold">Closed dates</h3>
        <Field label="One date per line (YYYY-MM-DD)"><Textarea rows={4} value={v.closed_dates.join("\n")} onChange={(e) => setV({ ...v, closed_dates: e.target.value.split("\n") })} /></Field></Card>
      <Card className="grid gap-3 p-5 sm:grid-cols-2"><h3 className="text-sm font-semibold sm:col-span-2">Booking</h3>
        {Object.keys(BOOKING_LABELS).map((k) => (
          <Field key={k} label={BOOKING_LABELS[k]}><Input type="number" min={0} value={v.booking[k] ?? ""} onChange={(e) => setV({ ...v, booking: { ...v.booking, [k]: Number(e.target.value) } })} /></Field>
        ))}</Card>
      <Card className="grid gap-3 p-5 sm:grid-cols-2"><h3 className="text-sm font-semibold sm:col-span-2">Cancelling and moving</h3>
        <Field label="Online cancellation notice (hours)"><Input type="number" min={0} value={v.cancellation.min_notice_hours} onChange={(e) => setV({ ...v, cancellation: { ...v.cancellation, min_notice_hours: Number(e.target.value) } })} /></Field>
        <Field label="Online reschedule notice (hours)"><Input type="number" min={0} value={v.reschedule.min_notice_hours} onChange={(e) => setV({ ...v, reschedule: { min_notice_hours: Number(e.target.value) } })} /></Field>
        <Field label="Cancellation policy (told to patients)" className="sm:col-span-2"><Textarea rows={3} value={v.cancellation.policy_text} onChange={(e) => setV({ ...v, cancellation: { ...v.cancellation, policy_text: e.target.value } })} /></Field></Card>
      <Card className="grid gap-3 p-5"><h3 className="text-sm font-semibold">Safety keywords</h3>
        <p className="text-xs text-muted-foreground">Messages containing these words skip the AI: emergencies get the 112 message, medical questions go to staff. One per line, English and German.</p>
        <div className="grid gap-3 sm:grid-cols-3">
          {Object.entries(v.escalation_keywords).map(([cat, words]) => (
            <Field key={cat} label={cat.replace("_", " ")}><Textarea rows={8} value={words.join("\n")}
              onChange={(e) => setV({ ...v, escalation_keywords: { ...v.escalation_keywords, [cat]: e.target.value.split("\n") } })} /></Field>
          ))}
        </div></Card>
      <div><Button onClick={save}>Save rules</Button></div>
    </div>
  );
}

interface User { id: number; email: string; name: string; role: "admin" | "staff"; active: boolean; last_login_at: string | null }

function UsersTab({ me, run, onAuthError }: { me: Me; run: Run; onAuthError: () => void }) {
  const users = useApi(() => api<User[]>("/api/admin/users"), [], onAuthError);
  const [n, setN] = useState({ name: "", email: "", role: "staff", password: "" });
  const change = async (u: User, c: Partial<User> & { password?: string }) => {
    if (await run(() => patch(`/api/admin/users/${u.id}`, c), "User updated.")) users.refresh();
  };
  return (
    <div className="grid gap-4">
      <Card className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-border"><tr><th className={th}>Name</th><th className={th}>Email</th><th className={th}>Role</th><th className={th}>Last sign-in</th><th className={th} /></tr></thead>
          <tbody className="divide-y divide-border">
            {(users.data ?? []).map((u) => (
              <tr key={u.id} className={u.active ? "" : "text-muted-foreground"}>
                <td className={td}>{u.name}{u.id === me.id && <Badge className="ml-2">you</Badge>}</td>
                <td className={td}>{u.email}</td>
                <td className={td}><Select className="w-28" value={u.role} onChange={(e) => change(u, { role: e.target.value as User["role"] })} aria-label="Role">
                  <option value="staff">staff</option><option value="admin">admin</option></Select></td>
                <td className={td + " text-muted-foreground"}>{when(u.last_login_at)}</td>
                <td className={td + " whitespace-nowrap text-right"}>
                  <Button size="sm" variant="ghost" onClick={() => { const p = prompt(`New password for ${u.email} (at least 10 characters):`); if (p) change(u, { password: p }); }}>Reset password</Button>
                  <Button size="sm" variant="ghost" onClick={() => change(u, { active: !u.active })}>{u.active ? "Deactivate" : "Activate"}</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card className="grid gap-3 p-5 sm:grid-cols-2">
        <h3 className="text-sm font-semibold sm:col-span-2">Add a user</h3>
        <Field label="Name"><Input value={n.name} onChange={(e) => setN({ ...n, name: e.target.value })} /></Field>
        <Field label="Email"><Input type="email" value={n.email} onChange={(e) => setN({ ...n, email: e.target.value })} /></Field>
        <Field label="Role" hint="Staff: calendar, chats, handoffs. Admin: also settings, users, doctors and documents.">
          <Select value={n.role} onChange={(e) => setN({ ...n, role: e.target.value })}><option value="staff">staff</option><option value="admin">admin</option></Select></Field>
        <Field label="First password" hint="Share it safely; they can change it under My password."><Input type="password" autoComplete="new-password" value={n.password} onChange={(e) => setN({ ...n, password: e.target.value })} /></Field>
        <div><Button onClick={async () => { if (await run(() => post("/api/admin/users", n), `${n.email} can now sign in.`)) { setN({ name: "", email: "", role: "staff", password: "" }); users.refresh(); } }}>
          <Plus className="h-4 w-4" />Add user</Button></div>
      </Card>
    </div>
  );
}

function DataTab({ s, run }: { s: AllSettings; run: Run }) {
  const [v, setV] = useState({ kind: s.data_source.kind, database_url: "", excel_path: "", google_sheets_id: s.data_source.google_sheets_id, google_service_account_file: "" });
  return (
    <Card className="grid gap-3 p-5">
      <p className="text-sm">Currently: <Badge>{s.data_source.kind}</Badge>
        {s.data_source.kind === "sqlite" && <span className="ml-2 text-muted-foreground">{s.data_source.sqlite_path}</span>}
        {s.data_source.kind === "excel" && <span className="ml-2 text-muted-foreground">{s.data_source.excel_path}</span>}</p>
      <Field label="Switch to">
        <Select value={v.kind} onChange={(e) => setV({ ...v, kind: e.target.value })}>
          <option value="sqlite">Built-in database (SQLite)</option><option value="postgres">Postgres / Supabase</option>
          <option value="excel">Excel file</option><option value="google_sheets">Google Sheets</option>
        </Select>
      </Field>
      {v.kind === "postgres" && <Field label="Connection string" hint={s.data_source.database_url_set ? "One is saved; leave empty to keep it." : undefined}><Input type="password" value={v.database_url} onChange={(e) => setV({ ...v, database_url: e.target.value })} /></Field>}
      {v.kind === "excel" && <Field label="Path to the .xlsx file"><Input value={v.excel_path} placeholder={s.data_source.excel_path} onChange={(e) => setV({ ...v, excel_path: e.target.value })} /></Field>}
      {v.kind === "google_sheets" && <>
        <Field label="Google Sheet id"><Input value={v.google_sheets_id} onChange={(e) => setV({ ...v, google_sheets_id: e.target.value })} /></Field>
        <Field label="Service account key file"><Input value={v.google_service_account_file} placeholder="config/google-service-account.json" onChange={(e) => setV({ ...v, google_service_account_file: e.target.value })} /></Field>
      </>}
      <Notice tone="warn">Switching needs a server restart. Staff accounts and conversations live in the data store, so set them up again in the new one.</Notice>
      <p className="text-xs text-muted-foreground">Knowledge base: {s.knowledge.embedding_provider} embeddings ({s.knowledge.embedding_model}), vectors in {s.knowledge.vector_store}.</p>
      <div><Button onClick={() => run(() => put("/api/admin/settings/data-source", Object.fromEntries(Object.entries(v).filter(([, x]) => x))), "Saved. Restart the server to switch.")}>Save</Button></div>
    </Card>
  );
}

function AccountTab({ me, run }: { me: Me; run: Run }) {
  const [v, setV] = useState({ current_password: "", new_password: "" });
  if (!me.id) return <Notice>You're signed in with the admin token. Sign in with a user account to change a password.</Notice>;
  return (
    <Card className="grid max-w-md gap-3 p-5">
      <Field label="Current password"><Input type="password" autoComplete="current-password" value={v.current_password} onChange={(e) => setV({ ...v, current_password: e.target.value })} /></Field>
      <Field label="New password" hint="At least 10 characters, mixing letters with numbers or symbols."><Input type="password" autoComplete="new-password" value={v.new_password} onChange={(e) => setV({ ...v, new_password: e.target.value })} /></Field>
      <div><Button onClick={() => run(() => post("/api/auth/password", v), "Password changed. Other sessions were signed out.")}>Change password</Button></div>
    </Card>
  );
}
