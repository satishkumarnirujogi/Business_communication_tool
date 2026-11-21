import { AlertTriangle, CalendarDays, CalendarPlus, CheckCircle2, FileText, Hash, MapPin, Plus, Send, Stethoscope, UserRound, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { downloadIcs, type BookingCard } from "./ics";
import { initialLang, TEXT, type Lang } from "./i18n";

interface Clinic { name: string; demo?: boolean; phone: string; address: string; timezone: string; languages: string[]; branding: { logo_url: string; primary_color: string } }
interface Slot { slot_id: number; label: string; start: string | null; doctor: string; duration_minutes?: number }
interface Ui { slots?: Slot[]; confirm?: boolean; booking?: BookingCard }
interface Msg {
  key: string; id?: number; role: "patient" | "assistant" | "staff" | "system"; text: string; author?: string;
  sources?: string[]; ui?: Ui; escalation?: { category: string } | null; pending?: boolean; status?: string;
}

const STORE = "clinic-chat-v1";
const embed = new URLSearchParams(location.search).has("embed");
let seq = 0;
const key = () => `m${Date.now()}-${seq++}`;

function load(): { sid: string | null; messages: Msg[]; lastId: number } {
  try {
    const v = JSON.parse(sessionStorage.getItem(STORE) ?? "null");
    if (v && Array.isArray(v.messages)) return { sid: v.sid, messages: v.messages.filter((m: Msg) => !m.pending), lastId: v.lastId ?? 0 };
  } catch {
    /* storage unavailable: start fresh */
  }
  return { sid: null, messages: [], lastId: 0 };
}

export default function ChatApp() {
  const saved = useMemo(load, []);
  const [clinic, setClinic] = useState<Clinic | null>(null);
  const [lang, setLang] = useState<Lang>("en");
  const [sid, setSid] = useState<string | null>(saved.sid);
  const [messages, setMessages] = useState<Msg[]>(saved.messages);
  const [lastId, setLastId] = useState(saved.lastId);
  const [human, setHuman] = useState(false);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const t = TEXT[lang];
  const tz = clinic?.timezone ?? "UTC";  // show times in the clinic's zone

  useEffect(() => {
    fetch("/api/clinic").then((r) => r.json()).then((c: Clinic) => {
      setClinic(c);
      setLang(initialLang(c.languages));
      document.title = c.name;
      if (c.branding?.primary_color) document.documentElement.style.setProperty("--color-primary", c.branding.primary_color);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE, JSON.stringify({ sid, lastId, messages: messages.filter((m) => !m.pending).slice(-60) }));
    } catch {
      /* ignore */
    }
  }, [sid, lastId, messages]);

  const firstScroll = useRef(true);
  useEffect(() => {
    // Jump to the latest message on load; scroll smoothly after that.
    const el = listRef.current;
    if (!el) return;
    requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: firstScroll.current ? "auto" : "smooth" }));
    firstScroll.current = false;
  }, [messages]);

  // Staff replies and notices arrive by polling (faster while a person has taken over).
  const poll = useCallback(async () => {
    if (!sid || busy) return;
    try {
      const r = await fetch(`/api/chat/${sid}/messages?after=${lastId}`);
      if (!r.ok) return;
      const data = await r.json();
      setHuman(data.status === "human");
      if (data.messages.length) {
        setMessages((prev) => {
          const known = new Set(prev.map((m) => m.id).filter(Boolean));
          const fresh = data.messages.filter((m: { id: number }) => !known.has(m.id)).map((m: Msg & { content: string }) => ({
            key: key(), id: m.id, role: m.role, text: m.content, author: m.author,
            sources: (m as unknown as { meta?: { sources?: string[] } }).meta?.sources ?? [],
          }));
          return fresh.length ? [...prev, ...fresh] : prev;
        });
      }
      setLastId(data.last_id);
    } catch {
      /* offline: try again later */
    }
  }, [sid, busy, lastId]);

  useEffect(() => {
    const timer = setInterval(poll, human ? 3000 : 8000);
    return () => clearInterval(timer);
  }, [poll, human]);

  // Check right away on load (e.g. staff may have taken over while the page was closed).
  const polledOnce = useRef(false);
  useEffect(() => {
    if (sid && !polledOnce.current) {
      polledOnce.current = true;
      poll();
    }
  }, [sid, poll]);

  const update = (k: string, fn: (m: Msg) => Msg) => setMessages((prev) => prev.map((m) => (m.key === k ? fn(m) : m)));

  async function send(text: string) {
    text = text.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    const replyKey = key();
    setMessages((prev) => [...prev, { key: key(), role: "patient", text }, { key: replyKey, role: "assistant", text: "", pending: true }]);
    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sid, language: lang }),
      });
      if (!res.ok || !res.body) throw new Error(String(res.status));
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";
        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;
          const ev = JSON.parse(part.slice(6));
          if (ev.type === "delta") update(replyKey, (m) => ({ ...m, text: m.text + ev.text, status: undefined }));
          else if (ev.type === "reset") update(replyKey, (m) => ({ ...m, text: "" }));
          else if (ev.type === "status") update(replyKey, (m) => ({ ...m, status: t.status[ev.tool] }));
          else if (ev.type === "error") update(replyKey, (m) => ({ ...m, text: ev.message, pending: false }));
          else if (ev.type === "done") {
            setSid(ev.session_id);
            setHuman(!!ev.human);
            if (ev.message_id) setLastId((id) => Math.max(id, ev.message_id));
            if (!ev.reply) setMessages((prev) => prev.filter((m) => m.key !== replyKey));
            else update(replyKey, () => ({
              key: replyKey, id: ev.message_id ?? undefined, role: "assistant", text: ev.reply, sources: ev.sources,
              ui: ev.ui, escalation: ev.escalation,
            }));
          }
        }
      }
    } catch {
      update(replyKey, (m) => ({ ...m, text: t.error, pending: false }));
    } finally {
      setMessages((prev) => prev.map((m) => (m.key === replyKey && m.pending ? { ...m, pending: false, text: m.text || t.error } : m)));
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  function restart() {
    setSid(null);
    setMessages([]);
    setLastId(0);
    setHuman(false);
  }

  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant" && !m.pending);
  const brand = clinic?.branding;

  return (
    <div className={"flex h-full flex-col bg-white " + (embed ? "" : "sm:bg-[#f4f8f8] sm:p-6")}>
      <div className={"mx-auto flex h-full w-full flex-col overflow-hidden bg-white " + (embed ? "" : "sm:max-w-2xl sm:rounded-2xl sm:border sm:border-border sm:shadow-sm")}>
        {/* Header */}
        <header className="flex items-center gap-3 border-b border-border px-4 py-3">
          {brand?.logo_url ? (
            <img src={brand.logo_url} alt="" className="h-9 w-9 rounded-xl object-cover" />
          ) : (
            <span className="grid h-9 w-9 place-items-center rounded-xl bg-primary-soft text-primary"><Stethoscope className="h-5 w-5" /></span>
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 truncate text-[15px] font-semibold">{clinic?.name ?? "…"}
              {clinic?.demo && <span className="rounded-full bg-amber-100 px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-amber-800">Demo</span>}</div>
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className={"h-1.5 w-1.5 rounded-full " + (human ? "bg-amber-500" : "bg-emerald-500")} />
              {human ? t.human : t.online}
            </div>
          </div>
          {(clinic?.languages.length ?? 0) > 1 && (
            <div className="flex rounded-full border border-border p-0.5 text-xs font-medium" role="group" aria-label="Language">
              {(["de", "en"] as Lang[]).filter((l) => clinic?.languages.includes(l)).map((l) => (
                <button key={l} onClick={() => setLang(l)} aria-pressed={lang === l}
                        className={"rounded-full px-2.5 py-1 " + (lang === l ? "bg-primary text-white" : "text-muted-foreground")}>
                  {l.toUpperCase()}
                </button>
              ))}
            </div>
          )}
          {messages.length > 0 && (
            <button onClick={restart} title={t.newChat} aria-label={t.newChat} className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted">
              <Plus className="h-4 w-4" />
            </button>
          )}
          {embed && (
            <button onClick={() => window.parent.postMessage({ type: "clinic-chat:close" }, "*")} aria-label={t.close}
                    className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted">
              <X className="h-4 w-4" />
            </button>
          )}
        </header>

        {/* Messages */}
        <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4" aria-live="polite">
          <Bubble role="assistant" text={t.greeting} />
          {messages.length === 0 && (
            <div className="flex flex-wrap gap-2 pl-1">
              {t.chips.map((label, i) => (
                <button key={label} onClick={() => send(t.chipMessages[i])}
                        className="rounded-full border border-primary/30 bg-primary-soft px-3 py-1.5 text-sm font-medium text-primary hover:border-primary">
                  {label}
                </button>
              ))}
            </div>
          )}
          {messages.map((m) => (
            <div key={m.key} className="space-y-2">
              {m.pending && !m.text ? <Typing status={m.status} /> : <Bubble role={m.role} text={m.text} author={m.author ?? t.team} escalation={m.escalation} />}
              {m.sources && m.sources.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5 pl-1 text-xs text-muted-foreground">
                  <span>{t.sources}:</span>
                  {m.sources.map((s) => (
                    <span key={s} className="inline-flex items-center gap-1 rounded-full border border-border bg-white px-2 py-0.5">
                      <FileText className="h-3 w-3" />{s}
                    </span>
                  ))}
                </div>
              )}
              {m.ui?.booking && <Booking card={m.ui.booking} t={t} tz={tz} lang={lang} />}
              {m === lastAssistant && m.ui?.slots && !busy && (
                <div className="flex flex-wrap gap-2 pl-1">
                  {m.ui.slots.map((s) => (
                    <button key={s.slot_id} onClick={() => send(t.pickTime(s.label, s.doctor))}
                            className="rounded-xl border border-border bg-white px-3 py-2 text-left text-sm shadow-sm hover:border-primary hover:bg-primary-soft">
                      <div className="font-semibold">{s.start ? new Date(s.start).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: tz }) : s.label}</div>
                      <div className="text-xs text-muted-foreground">{s.start ? new Date(s.start).toLocaleDateString(lang, { weekday: "short", day: "numeric", month: "short", timeZone: tz }) : ""} · {s.doctor}</div>
                    </button>
                  ))}
                </div>
              )}
              {m === lastAssistant && m.ui?.confirm && !busy && (
                <div className="flex gap-2 pl-1">
                  <button onClick={() => send(t.yesMessage)} className="rounded-full bg-primary px-4 py-1.5 text-sm font-medium text-white">{t.yes}</button>
                  <button onClick={() => send(t.changeMessage)} className="rounded-full border border-border px-4 py-1.5 text-sm font-medium">{t.change}</button>
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Composer */}
        <form onSubmit={(e) => { e.preventDefault(); send(input); }}
              className="border-t border-border bg-white px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
          <div className="flex items-end gap-2">
            <textarea ref={inputRef} rows={1} value={input} maxLength={2000} placeholder={t.placeholder} aria-label={t.placeholder}
                      onChange={(e) => setInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
                      className="max-h-32 min-h-[44px] flex-1 resize-none rounded-2xl border border-border bg-muted/40 px-4 py-2.5 text-[15px] outline-none focus:border-primary focus:bg-white" />
            <button type="submit" disabled={busy || !input.trim()} aria-label={t.send}
                    className="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-primary text-white disabled:opacity-40">
              <Send className="h-4 w-4" />
            </button>
          </div>
          <p className="mt-2 text-center text-[11px] text-muted-foreground">{t.notEmergency}</p>
        </form>
      </div>
    </div>
  );
}

function Bubble({ role, text, author, escalation }: { role: Msg["role"]; text: string; author?: string; escalation?: Msg["escalation"] }) {
  if (role === "system") {
    return <div className="mx-auto max-w-[85%] rounded-full bg-muted px-3 py-1 text-center text-xs text-muted-foreground">{text}</div>;
  }
  const mine = role === "patient";
  const urgent = escalation && ["urgent", "crisis"].includes(escalation.category);
  return (
    <div className={"flex " + (mine ? "justify-end" : "justify-start")}>
      <div className={
        "max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-[15px] leading-relaxed " +
        (mine ? "rounded-br-md bg-primary text-white"
          : urgent ? "rounded-bl-md border border-red-200 bg-red-50 text-red-900"
            : "rounded-bl-md bg-[#f1f5f5] text-foreground")
      }>
        {role === "staff" && <div className="mb-0.5 flex items-center gap-1 text-xs font-semibold text-primary"><UserRound className="h-3 w-3" />{author}</div>}
        {urgent && <AlertTriangle className="mb-1 h-4 w-4 text-red-600" />}
        {text}
      </div>
    </div>
  );
}

function Typing({ status }: { status?: string }) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex gap-1 rounded-2xl rounded-bl-md bg-[#f1f5f5] px-4 py-3" aria-label="typing">
        {[0, 1, 2].map((i) => (
          <span key={i} className="h-2 w-2 animate-bounce rounded-full bg-slate-400" style={{ animationDelay: `${i * 150}ms` }} />
        ))}
      </div>
      {status && <span className="text-xs text-muted-foreground">{status}</span>}
    </div>
  );
}

function Booking({ card, t, tz, lang }: { card: BookingCard; t: (typeof TEXT)[Lang]; tz: string; lang: Lang }) {
  const when = card.start
    ? new Date(card.start).toLocaleString(lang, { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: tz })
    : card.label;
  return (
    <div className="ml-1 max-w-[85%] overflow-hidden rounded-2xl border border-primary/30 bg-white shadow-sm">
      <div className="flex items-center gap-2 bg-primary-soft px-4 py-2 text-sm font-semibold text-primary">
        <CheckCircle2 className="h-4 w-4" />{t.booked}
      </div>
      <dl className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1.5 px-4 py-3 text-sm">
        {card.service && <><dt><Stethoscope className="h-4 w-4 text-muted-foreground" /></dt><dd className="font-medium">{card.service}</dd></>}
        <dt><CalendarDays className="h-4 w-4 text-muted-foreground" /></dt><dd>{when}</dd>
        {card.doctor && <><dt><UserRound className="h-4 w-4 text-muted-foreground" /></dt><dd>{card.doctor}</dd></>}
        {card.address && <><dt><MapPin className="h-4 w-4 text-muted-foreground" /></dt><dd>{card.clinic}, {card.address}</dd></>}
        <dt><Hash className="h-4 w-4 text-muted-foreground" /></dt><dd>{t.reference}: <span className="font-mono font-semibold">{card.reference}</span></dd>
      </dl>
      {card.start && (
        <button onClick={() => downloadIcs(card)} className="flex w-full items-center justify-center gap-2 border-t border-border px-4 py-2.5 text-sm font-medium text-primary hover:bg-primary-soft">
          <CalendarPlus className="h-4 w-4" />{t.addToCalendar}
        </button>
      )}
    </div>
  );
}
