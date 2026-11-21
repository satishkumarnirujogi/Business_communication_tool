import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Empty, Input, Notice, PageHeader, Select, td, th } from "@/components/ui/form";
import { api, type AuditRow } from "@/lib/api";
import { when } from "../shared";
import { useApi } from "../useApi";

const DECISION: Record<string, string> = {
  allowed: "border-emerald-200 bg-emerald-50 text-emerald-800", denied: "border-amber-200 bg-amber-50 text-amber-800",
  error: "border-red-200 bg-red-50 text-red-700", escalated: "border-violet-200 bg-violet-50 text-violet-800",
};
const PAGE = 50;

export default function AuditPage({ onAuthError }: { onAuthError: () => void }) {
  const [form, setForm] = useState({ q: "", action: "", decision: "", actor: "", since: "", until: "" });
  const [filters, setFilters] = useState(form);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset),
    ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)) });
  const { data, error } = useApi(() => api<AuditRow[]>(`/api/audit?${params}`), [params.toString()], onAuthError);

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader title="Audit log" description="Every assistant action, rule decision and staff change. Emails, phone numbers and birth dates are masked." />
      <form className="mb-3 grid gap-2 sm:grid-cols-3 lg:grid-cols-7" onSubmit={(e) => { e.preventDefault(); setOffset(0); setFilters(form); }}>
        <div className="relative sm:col-span-2"><Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input className="pl-8" placeholder="Search reason, rule, details…" value={form.q} onChange={(e) => setForm({ ...form, q: e.target.value })} aria-label="Search" /></div>
        <Input placeholder="Action, e.g. book" value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })} aria-label="Action" />
        <Select value={form.decision} onChange={(e) => setForm({ ...form, decision: e.target.value })} aria-label="Decision">
          <option value="">Any decision</option>{Object.keys(DECISION).map((d) => <option key={d}>{d}</option>)}
        </Select>
        <Input type="date" value={form.since} onChange={(e) => setForm({ ...form, since: e.target.value })} aria-label="From" />
        <Input type="date" value={form.until} onChange={(e) => setForm({ ...form, until: e.target.value })} aria-label="Until" />
        <Button type="submit">Filter</Button>
      </form>
      {error && <Notice tone="error">{error}</Notice>}
      {data && data.length === 0 && <Empty>No entries match.</Empty>}
      {data && data.length > 0 && (
        <Card className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-border"><tr><th className={th} /><th className={th}>Time</th><th className={th}>Action</th><th className={th}>Decision</th><th className={th}>Actor</th><th className={th}>Rule / reason</th><th className={th}>Conversation</th></tr></thead>
            <tbody className="divide-y divide-border">
              {data.map((r) => (
                <Fragment key={r.id}>
                  <tr className="cursor-pointer hover:bg-muted/50" onClick={() => setOpen(open === r.id ? null : r.id)}>
                    <td className={td}>{open === r.id ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}</td>
                    <td className={td + " whitespace-nowrap tabular-nums"}>{when(r.created_at, "d LLL, HH:mm:ss")}</td>
                    <td className={td + " font-medium"}>{r.action}</td>
                    <td className={td}><Badge className={DECISION[r.decision]}>{r.decision}</Badge></td>
                    <td className={td}>{r.actor}</td>
                    <td className={td + " max-w-md"}><div className="truncate">{[r.rule, r.reason].filter(Boolean).join(" — ") || "—"}</div></td>
                    <td className={td}>{r.session_id ? <a className="font-mono text-xs text-primary hover:underline" href={`#/conversations/${r.session_id}`} onClick={(e) => e.stopPropagation()}>{r.session_id.slice(0, 8)}</a> : "—"}</td>
                  </tr>
                  {open === r.id && (
                    <tr className="bg-muted/40"><td colSpan={7} className="px-4 py-3">
                      <div className="grid gap-3 text-xs md:grid-cols-2">
                        <div><div className="mb-1 font-medium">Inputs</div><pre className="overflow-x-auto whitespace-pre-wrap rounded bg-white p-2">{JSON.stringify(r.params, null, 2)}</pre></div>
                        <div><div className="mb-1 font-medium">Result</div><pre className="overflow-x-auto whitespace-pre-wrap rounded bg-white p-2">{JSON.stringify(r.result, null, 2)}</pre></div>
                        {r.reason && <div className="md:col-span-2"><span className="font-medium">Reason:</span> {r.reason}</div>}
                      </div>
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </Card>
      )}
      <div className="mt-3 flex justify-between">
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</Button>
        <Button variant="outline" size="sm" disabled={(data?.length ?? 0) < PAGE} onClick={() => setOffset(offset + PAGE)}>Older</Button>
      </div>
    </div>
  );
}
