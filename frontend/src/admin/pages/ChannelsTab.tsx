import { CheckCircle2, Copy, Loader2, Mail, MessageCircle, Send, XCircle } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Field, Input, Notice, Select, td, th } from "@/components/ui/form";
import { api, post, put } from "@/lib/api";
import { when } from "../shared";
import { attempt, useApi } from "../useApi";

type Value = string | number | boolean;
interface Status {
  public_base_url: string;
  whatsapp: { provider: string; label: string; configured: boolean; problems: string[]; webhook_url: string; reminder_template_set: boolean };
  email: { enabled: boolean; configured: boolean; can_send: boolean; problems: string[] };
  reminders: { enabled: boolean; send_time: string; min_hours: number };
  values: Record<string, Value>;
  secrets_set: Record<string, boolean>;
}
interface ReminderRow {
  reference: string; patient: string; doctor: string; service: string | null; start: string; booked_via: string;
  due_at: string; status: string; sent_via: string; error: string; attempts: number;
}
type Run = <T>(fn: () => Promise<T>, message: string) => Promise<T | undefined>;

const STATUS_TONE: Record<string, string> = {
  sent: "border-emerald-200 bg-emerald-50 text-emerald-800", failed: "border-red-200 bg-red-50 text-red-700",
  skipped: "border-amber-200 bg-amber-50 text-amber-800", pending: "",
};

function Ready({ ok, problems }: { ok: boolean; problems: string[] }) {
  return ok
    ? <span className="inline-flex items-center gap-1 text-sm text-emerald-700"><CheckCircle2 className="h-4 w-4" />Ready</span>
    : <div className="text-sm text-amber-800">{problems.map((p) => <div key={p} className="flex gap-1"><XCircle className="mt-0.5 h-4 w-4 shrink-0" />{p}</div>)}</div>;
}

export default function ChannelsTab({ run, onAuthError }: { run: Run; onAuthError: () => void }) {
  const status = useApi(() => api<Status>("/api/admin/channels"), [], onAuthError);
  if (!status.data) return status.error ? <Notice tone="error">{status.error}</Notice> : <Loader2 className="h-5 w-5 animate-spin" />;
  return <ChannelsForm key={JSON.stringify(status.data.values)} s={status.data} run={run} onSaved={status.refresh} onAuthError={onAuthError} />;
}

function ChannelsForm({ s, run, onSaved, onAuthError }: { s: Status; run: Run; onSaved: () => void; onAuthError: () => void }) {
  const [v, setV] = useState<Record<string, Value>>(s.values);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [testTo, setTestTo] = useState({ whatsapp: "", email: "" });
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState("");
  const set = (k: string, x: Value) => setV({ ...v, [k]: x });
  const text = (k: string, label: string, hint?: string, placeholder?: string) => (
    <Field label={label} hint={hint}><Input value={String(v[k] ?? "")} placeholder={placeholder} onChange={(e) => set(k, e.target.value)} /></Field>
  );
  const secret = (k: string, label: string, hint?: string) => (
    <Field label={label} hint={s.secrets_set[k] ? "Saved. Leave empty to keep it; it is never shown again." : hint ?? "Not set yet."}>
      <Input type="password" autoComplete="off" value={secrets[k] ?? ""} onChange={(e) => setSecrets({ ...secrets, [k]: e.target.value })} />
    </Field>
  );
  const save = async () => {
    const r = await run(() => put("/api/admin/settings/channels", { values: { ...v, ...secrets } }), "Channel settings saved. They apply right away.");
    if (r) { setSecrets({}); onSaved(); }
  };
  const test = async (channel: "whatsapp" | "email") => {
    setBusy(channel); setTestMsg(null);
    let err = "";
    const r = await attempt(() => post("/api/admin/channels/test", { channel, to: testTo[channel] }), (m) => { err = m; }, onAuthError);
    setTestMsg(r ? { ok: true, text: "Sent. Check the phone or inbox." } : { ok: false, text: err });
    setBusy("");
  };
  const provider = String(v.whatsapp_provider);

  return (
    <div className="grid gap-5">
      <Card className="grid gap-3 p-5">
        <h2 className="font-semibold">Public address</h2>
        {text("public_base_url", "Address of this server on the internet", "WhatsApp needs it to reach the assistant, e.g. https://assistant.your-clinic.example. For a test on your computer, a tunnel such as ngrok or cloudflared works.", "https://assistant.your-clinic.example")}
      </Card>

      <Card className="grid gap-3 p-5">
        <div className="flex items-center gap-2"><MessageCircle className="h-5 w-5 text-primary" /><h2 className="font-semibold">WhatsApp</h2>
          <span className="ml-auto">{provider !== "none" && <Ready ok={s.whatsapp.configured} problems={s.whatsapp.problems} />}</span></div>
        <Field label="Provider" hint="Use the clinic's own WhatsApp Business number. See docs/whatsapp.md for a step-by-step guide.">
          <Select value={provider} onChange={(e) => set("whatsapp_provider", e.target.value)}>
            <option value="none">Off</option><option value="twilio">Twilio</option><option value="meta">Meta WhatsApp Cloud API</option>
          </Select>
        </Field>
        {provider === "twilio" && <>
          {text("twilio_account_sid", "Account SID", undefined, "AC…")}
          {secret("twilio_auth_token", "Auth token")}
          {text("twilio_whatsapp_number", "Clinic WhatsApp number", "International format", "+4930123456")}
          {text("twilio_reminder_content_sid", "Reminder template (Content SID)", "An approved template with 4 variables: name, date and time, doctor, reference.", "HX…")}
        </>}
        {provider === "meta" && <>
          {secret("whatsapp_access_token", "Access token", "A permanent system-user token with whatsapp_business_messaging.")}
          {text("whatsapp_phone_number_id", "Phone number ID")}
          {secret("whatsapp_app_secret", "App secret", "Used to check that webhooks really come from Meta.")}
          {secret("whatsapp_verify_token", "Verify token", "Any long random text. Enter the same text in the Meta webhook settings.")}
          {text("whatsapp_reminder_template", "Reminder template name", "Approved in English and German, with 4 body variables: name, date and time, doctor, reference.", "appointment_reminder")}
          {text("whatsapp_graph_version", "Graph API version")}
        </>}
        {provider !== "none" && s.whatsapp.provider === provider && s.whatsapp.webhook_url && (
          <div className="rounded-lg bg-muted p-3 text-sm">
            <div className="mb-1 font-medium">Webhook URL to enter at {provider === "twilio" ? "Twilio (“When a message comes in”, POST)" : "Meta (WhatsApp → Configuration)"}</div>
            <div className="flex items-center gap-2"><code className="break-all">{s.whatsapp.webhook_url}</code>
              <Button size="icon" variant="ghost" aria-label="Copy" onClick={() => navigator.clipboard.writeText(s.whatsapp.webhook_url)}><Copy className="h-4 w-4" /></Button></div>
            {!s.public_base_url && <div className="mt-1 text-amber-800">Set the public address above first, so this is a full link.</div>}
          </div>
        )}
        {provider !== "none" && s.whatsapp.provider === provider && (
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Send a test to"><Input className="w-56" placeholder="+49170…" value={testTo.whatsapp} onChange={(e) => setTestTo({ ...testTo, whatsapp: e.target.value })} /></Field>
            <Button variant="outline" disabled={!!busy || !testTo.whatsapp} onClick={() => test("whatsapp")}>{busy === "whatsapp" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}Send test</Button>
            <span className="pb-2 text-xs text-muted-foreground">WhatsApp only delivers it if that number messaged the clinic in the last 24 hours.</span>
          </div>
        )}
      </Card>

      <Card className="grid gap-3 p-5">
        <div className="flex items-center gap-2"><Mail className="h-5 w-5 text-primary" /><h2 className="font-semibold">Email</h2>
          <span className="ml-auto">{v.email_enabled ? <Ready ok={s.email.configured} problems={s.email.problems} /> : null}</span></div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!v.email_enabled} onChange={(e) => set("email_enabled", e.target.checked)} />
          Answer emails to the clinic inbox, and send reminders by email</label>
        {v.email_enabled && <>
          <div className="grid gap-3 sm:grid-cols-2">
            {text("email_address", "Clinic email address", undefined, "praxis@your-clinic.example")}
            {text("email_display_name", "Sender name", undefined, "Riverside Family Clinic")}
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            {text("imap_host", "IMAP server (incoming)", undefined, "imap.your-provider.example")}
            {text("imap_port", "IMAP port")}
            {text("imap_folder", "Folder")}
            {text("imap_user", "IMAP user", "Usually the email address")}
            {secret("imap_password", "IMAP password", "An app password if your provider offers one.")}
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            {text("smtp_host", "SMTP server (outgoing)", undefined, "smtp.your-provider.example")}
            {text("smtp_port", "SMTP port")}
            <Field label="Security"><Select value={String(v.smtp_security)} onChange={(e) => set("smtp_security", e.target.value)}>
              <option value="starttls">STARTTLS (port 587)</option><option value="ssl">SSL/TLS (port 465)</option><option value="none">None</option></Select></Field>
            {text("smtp_user", "SMTP user", "Usually the email address")}
            {secret("smtp_password", "SMTP password")}
            {text("email_poll_seconds", "Check inbox every (seconds)")}
          </div>
          <Notice tone="warn">Use a dedicated inbox: the assistant reads every unread email in that folder. Newsletters, auto-replies and bounces are ignored.</Notice>
          {s.email.can_send && (
            <div className="flex flex-wrap items-end gap-2">
              <Field label="Send a test to"><Input className="w-64" type="email" value={testTo.email} onChange={(e) => setTestTo({ ...testTo, email: e.target.value })} /></Field>
              <Button variant="outline" disabled={!!busy || !testTo.email} onClick={() => test("email")}>{busy === "email" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}Send test</Button>
            </div>
          )}
        </>}
      </Card>

      {testMsg && <Notice tone={testMsg.ok ? "ok" : "error"} onClose={() => setTestMsg(null)}>{testMsg.text}</Notice>}

      <Card className="grid gap-3 p-5">
        <h2 className="font-semibold">Appointment reminders</h2>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!v.reminders_enabled} onChange={(e) => set("reminders_enabled", e.target.checked)} />
          Send a reminder the day before each appointment</label>
        <div className="grid gap-3 sm:grid-cols-2">
          {text("reminder_send_time", "Send at (clinic time, the day before)", undefined, "10:00")}
          {text("reminder_min_hours", "Skip if booked less than this many hours ahead")}
        </div>
        <p className="text-xs text-muted-foreground">Reminders go back through the channel the patient booked on. Bookings from the web chat or by staff get an email, if email is set up.</p>
      </Card>

      <div><Button onClick={save}>Save channel settings</Button></div>

      <RemindersList run={run} onAuthError={onAuthError} />
    </div>
  );
}

function RemindersList({ run, onAuthError }: { run: Run; onAuthError: () => void }) {
  const list = useApi(() => api<{ enabled: boolean; reminders: ReminderRow[] }>("/api/admin/reminders?days=3"), [], onAuthError, 60000);
  return (
    <Card className="overflow-x-auto">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <h2 className="font-semibold">Upcoming reminders (next 3 days)</h2>
        <Button className="ml-auto" size="sm" variant="outline"
                onClick={async () => { const r = await run(() => post<{ sent: number; failed: number; skipped: number }>("/api/admin/reminders/run"), "Reminder check finished."); if (r) list.refresh(); }}>
          Send due reminders now</Button>
      </div>
      {list.error && <div className="p-3"><Notice tone="error">{list.error}</Notice></div>}
      {list.data && list.data.reminders.length === 0 && <p className="p-4 text-sm text-muted-foreground">No appointments in the next three days.</p>}
      {list.data && list.data.reminders.length > 0 && (
        <table className="w-full text-sm">
          <thead className="border-b border-border"><tr><th className={th}>Appointment</th><th className={th}>Patient</th><th className={th}>Booked via</th><th className={th}>Reminder</th></tr></thead>
          <tbody className="divide-y divide-border">
            {list.data.reminders.map((r) => (
              <tr key={r.reference}>
                <td className={td}><div className="font-medium">{when(r.start)}</div><div className="text-xs text-muted-foreground">{r.doctor}{r.service ? ` · ${r.service}` : ""} · {r.reference}</div></td>
                <td className={td}>{r.patient}</td>
                <td className={td}><Badge>{r.booked_via}</Badge></td>
                <td className={td}>
                  <Badge className={STATUS_TONE[r.status] ?? ""}>{r.status}{r.sent_via ? ` · ${r.sent_via}` : ""}</Badge>
                  {r.status === "pending" && <div className="text-xs text-muted-foreground">due {when(r.due_at)}</div>}
                  {r.error && <div className="text-xs text-red-700">{r.error}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
