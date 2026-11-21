import { ArrowLeft, Bot, FileText, Globe, Hand, Mail, MessageCircle, Search, Send, UserRound, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Empty, Input, Notice, PageHeader, Select, Textarea } from "@/components/ui/form";
import { api, post, type Conversation, type ConversationMessage, type Task } from "@/lib/api";
import { ago, when } from "../shared";
import { attempt, useApi } from "../useApi";

function ChannelIcon({ channel }: { channel: string }) {
  const Icon = channel === "whatsapp" ? MessageCircle : channel === "email" ? Mail : Globe;
  return <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-label={channel} />;
}

const STATUS_BADGE: Record<string, string> = {
  bot: "Assistant", human: "Staff answering", closed: "Closed",
};

export default function ConversationsPage({ selected, onAuthError }: { selected: string; onAuthError: () => void }) {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const list = useApi(
    () => api<Conversation[]>(`/api/admin/conversations?${new URLSearchParams({ q: query, ...(status ? { status } : {}) })}`),
    [query, status], onAuthError, 15000);

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader title="Conversations" description="Every chat with the assistant. Take one over to reply yourself." />
      <div className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <div className={selected ? "hidden xl:block" : ""}>
          <form className="mb-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); setQuery(q); }}>
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input className="pl-8" placeholder="Search messages, names…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
            </div>
            <Select className="w-32" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
              <option value="">All</option><option value="human">Staff</option><option value="bot">Assistant</option><option value="closed">Closed</option>
            </Select>
          </form>
          {list.error && <Notice tone="error">{list.error}</Notice>}
          {list.data && list.data.length === 0 && <Empty>No conversations{query ? " match" : " yet"}.</Empty>}
          <Card className="divide-y divide-border overflow-hidden">
            {(list.data ?? []).map((c) => (
              <a key={c.id} href={`#/conversations/${c.id}`}
                 className={"block px-3 py-2.5 text-sm hover:bg-muted " + (c.id === selected ? "bg-primary-soft" : "")}>
                <div className="flex items-center gap-2">
                  <ChannelIcon channel={c.channel} />
                  <span className="truncate font-medium">{c.patient_name || "Patient"}</span>
                  <span className="text-xs uppercase text-muted-foreground">{c.language}</span>
                  {c.status === "human" && <Badge className="border-amber-200 bg-amber-50 text-amber-800">Staff</Badge>}
                  {(c.open_tasks ?? 0) > 0 && <Badge className="border-red-200 bg-red-50 text-red-700">Handoff</Badge>}
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">{ago(c.last_message_at)}</span>
                </div>
                <div className="mt-0.5 truncate text-muted-foreground">{c.last_message}</div>
              </a>
            ))}
          </Card>
        </div>
        <div className={selected ? "" : "hidden xl:block"}>
          {selected ? <Transcript key={selected} id={selected} onAuthError={onAuthError} onChange={list.refresh} />
            : <Empty>Choose a conversation.</Empty>}
        </div>
      </div>
    </div>
  );
}

interface Detail { conversation: Conversation; messages: ConversationMessage[]; tasks: Task[] }

function Transcript({ id, onAuthError, onChange }: { id: string; onAuthError: () => void; onChange: () => void }) {
  const { data, error, refresh, setError } = useApi(() => api<Detail>(`/api/admin/conversations/${id}`), [id], onAuthError, 4000);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [data?.messages.length]);

  async function act(path: string, body?: unknown) {
    setBusy(true);
    const ok = await attempt(() => post<{ delivery_error?: string | null }>(`/api/admin/conversations/${id}/${path}`, body), setError, onAuthError);
    if (ok && ok.delivery_error) setError(ok.delivery_error);
    setBusy(false);
    if (ok) { refresh(); onChange(); }
    return ok;
  }

  if (!data) return error ? <Notice tone="error">{error}</Notice> : <Empty>Loading…</Empty>;
  const c = data.conversation;
  const human = c.status === "human";

  return (
    <Card className="flex h-[calc(100vh-11rem)] min-h-[480px] flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        <a href="#/conversations" className="xl:hidden" aria-label="Back"><ArrowLeft className="h-4 w-4" /></a>
        <div className="min-w-0">
          <div className="truncate font-medium">{c.patient_name || "Patient"} <span className="text-xs uppercase text-muted-foreground">{c.language}</span></div>
          <div className="text-xs text-muted-foreground">{c.channel === "whatsapp" ? "WhatsApp" : c.channel === "email" ? "Email" : "Web chat"} · Started {when(c.started_at)} · {c.message_count} messages · {STATUS_BADGE[c.status]}
            {human && c.taken_over_by ? ` (${c.taken_over_by})` : ""}</div>
        </div>
        <div className="ml-auto flex gap-2">
          {!human && <Button size="sm" onClick={() => act("takeover")} disabled={busy}><Hand className="h-4 w-4" />Take over</Button>}
          {human && <Button size="sm" variant="outline" onClick={() => act("release")} disabled={busy}><Bot className="h-4 w-4" />Hand back to assistant</Button>}
          {c.status !== "closed" && <Button size="sm" variant="ghost" onClick={() => act("close")} disabled={busy}><XCircle className="h-4 w-4" />Close</Button>}
        </div>
      </div>
      {data.tasks.length > 0 && (
        <div className="border-b border-border bg-amber-50 px-4 py-2 text-xs text-amber-900">
          {data.tasks.map((t) => <div key={t.id}>Handoff ({t.status.replace("_", " ")}): {t.summary}</div>)}
        </div>
      )}
      {error && <div className="px-4 pt-3"><Notice tone="error" onClose={() => setError("")}>{error}</Notice></div>}
      <div className="flex-1 space-y-2.5 overflow-y-auto px-4 py-4">
        {data.messages.map((m) => <Line key={m.id} m={m} />)}
        <div ref={end} />
      </div>
      <form className="flex items-end gap-2 border-t border-border p-3"
            onSubmit={async (e) => { e.preventDefault(); if (text.trim() && await act("reply", { text })) setText(""); }}>
        <Textarea rows={2} className="flex-1" value={text} maxLength={2000} onChange={(e) => setText(e.target.value)}
                  placeholder={human ? "Write to the patient…" : "Writing here takes the conversation over from the assistant"}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit(); }}
                  aria-label="Reply" />
        <Button type="submit" disabled={busy || !text.trim()}><Send className="h-4 w-4" />Send</Button>
      </form>
    </Card>
  );
}

function Line({ m }: { m: ConversationMessage }) {
  if (m.role === "system") return <div className="text-center text-xs text-muted-foreground">{m.content}</div>;
  const patient = m.role === "patient";
  const esc = m.meta?.escalation?.category;
  return (
    <div className={"flex " + (patient ? "justify-start" : "justify-end")}>
      <div className={"max-w-[80%] rounded-2xl px-3.5 py-2 text-sm " +
        (patient ? "rounded-bl-md bg-muted" : m.role === "staff" ? "rounded-br-md bg-amber-100" : "rounded-br-md bg-primary-soft")}>
        <div className="mb-0.5 flex items-center gap-1 text-[11px] font-medium text-muted-foreground">
          {patient ? <UserRound className="h-3 w-3" /> : m.role === "staff" ? <Hand className="h-3 w-3" /> : <Bot className="h-3 w-3" />}
          {patient ? "Patient" : m.role === "staff" ? m.author || "Staff" : "Assistant"} · {when(m.created_at, "HH:mm")}
          {esc && <span className="ml-1 rounded bg-red-100 px-1 text-red-700">{esc}</span>}
        </div>
        <div className="whitespace-pre-wrap">{m.content}</div>
        {m.meta?.sources && m.meta.sources.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">{m.meta.sources.map((s) => (
            <span key={s} className="inline-flex items-center gap-1 rounded-full bg-white px-1.5 text-[11px] text-muted-foreground"><FileText className="h-3 w-3" />{s}</span>
          ))}</div>
        )}
      </div>
    </div>
  );
}
