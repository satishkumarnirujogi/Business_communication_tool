import { AlertTriangle, CalendarCheck, Inbox, MessagesSquare, Sparkles } from "lucide-react";
import { Card, Empty, Notice, PageHeader } from "@/components/ui/form";
import { api } from "@/lib/api";
import { when } from "../shared";
import { useApi } from "../useApi";

interface Overview {
  date: string;
  appointments_today: { reference: string; start: string; end: string; patient: string; doctor: string; service: string | null; color: string }[];
  open_handoffs: number; urgent_handoffs: number; conversations_today: number; resolved_automatically_today: number;
  resolved_automatically_percent: number | null; live_takeovers: number;
}

export default function OverviewPage({ onAuthError }: { onAuthError: () => void }) {
  const { data, error } = useApi(() => api<Overview>("/api/admin/overview"), [], onAuthError, 30000);

  const stats = data ? [
    { label: "Appointments today", value: data.appointments_today.length, icon: CalendarCheck, href: "#/calendar" },
    { label: "Open handoffs", value: data.open_handoffs, icon: Inbox, href: "#/handoffs",
      note: data.urgent_handoffs ? `${data.urgent_handoffs} urgent` : undefined, urgent: data.urgent_handoffs > 0 },
    { label: "Conversations today", value: data.conversations_today, icon: MessagesSquare, href: "#/conversations",
      note: data.live_takeovers ? `${data.live_takeovers} with staff now` : undefined },
    { label: "Resolved automatically", value: data.resolved_automatically_percent == null ? "—" : `${data.resolved_automatically_percent}%`,
      icon: Sparkles, note: `${data.resolved_automatically_today} of ${data.conversations_today} today`, href: "#/conversations" },
  ] : [];

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="Overview" description={data ? when(data.date + "T00:00", "cccc d LLLL yyyy") : undefined} />
      {error && <Notice tone="error">{error}</Notice>}
      {data && data.urgent_handoffs > 0 && (
        <a href="#/handoffs" className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm font-medium text-red-800">
          <AlertTriangle className="h-4 w-4" /> {data.urgent_handoffs} urgent handoff{data.urgent_handoffs > 1 ? "s" : ""} waiting: possible emergency in chat.
        </a>
      )}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map((s) => (
          <a key={s.label} href={s.href}>
            <Card className="p-4 transition-colors hover:border-primary/50">
              <div className="flex items-center justify-between text-sm text-muted-foreground">{s.label}<s.icon className="h-4 w-4 text-primary" /></div>
              <div className="mt-2 text-3xl font-semibold">{s.value}</div>
              {s.note && <div className={"mt-1 text-xs " + ("urgent" in s && s.urgent ? "font-medium text-red-700" : "text-muted-foreground")}>{s.note}</div>}
            </Card>
          </a>
        ))}
      </div>
      <h2 className="mb-2 mt-6 text-sm font-semibold">Today's appointments</h2>
      {data && data.appointments_today.length === 0 && <Empty>No appointments today.</Empty>}
      {data && data.appointments_today.length > 0 && (
        <Card className="divide-y divide-border">
          {data.appointments_today.map((a) => (
            <div key={a.reference} className="flex items-center gap-3 px-4 py-2.5 text-sm">
              <span className="h-8 w-1 rounded-full" style={{ background: a.color || "#0f9d94" }} />
              <span className="w-28 font-medium tabular-nums">{when(a.start, "HH:mm")}–{when(a.end, "HH:mm")}</span>
              <span className="flex-1 truncate">{a.patient}<span className="text-muted-foreground"> · {a.service ?? "Appointment"}</span></span>
              <span className="hidden truncate text-muted-foreground sm:block">{a.doctor}</span>
              <span className="font-mono text-xs text-muted-foreground">{a.reference}</span>
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
