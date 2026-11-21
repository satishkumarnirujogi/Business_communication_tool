import { AlertTriangle, MessagesSquare } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Empty, Notice, PageHeader, Select, Textarea } from "@/components/ui/form";
import { api, patch, type Task } from "@/lib/api";
import { ago, when } from "../shared";
import { attempt, useApi } from "../useApi";

const KIND: Record<string, string> = {
  urgent: "Possible emergency", crisis: "Crisis", medical_question: "Medical question", callback: "Callback",
  question: "Question", complaint: "Complaint", other: "Other",
};
const STATUS = { open: "Open", in_progress: "In progress", done: "Done" } as const;

export default function HandoffsPage({ onAuthError }: { onAuthError: () => void }) {
  const [filter, setFilter] = useState<"active" | "open" | "in_progress" | "done">("active");
  const { data, error, refresh, setError } = useApi(
    () => api<Task[]>(`/api/admin/tasks${filter === "active" ? "" : `?status=${filter}`}`), [filter], onAuthError, 20000);
  const tasks = (data ?? []).filter((t) => filter !== "active" || t.status !== "done");

  async function update(t: Task, changes: Partial<Task>) {
    if (await attempt(() => patch(`/api/admin/tasks/${t.id}`, changes), setError, onAuthError)) refresh();
  }

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader title="Handoff queue" description="Things the assistant passed to a person. Urgent items first."
                  actions={
                    <Select className="w-40" value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)} aria-label="Show">
                      <option value="active">Open & in progress</option><option value="open">Open</option>
                      <option value="in_progress">In progress</option><option value="done">Done</option>
                    </Select>
                  } />
      {error && <Notice tone="error" onClose={() => setError("")}>{error}</Notice>}
      {data && tasks.length === 0 && <Empty>Nothing waiting. Nice.</Empty>}
      <div className="grid gap-3">
        {tasks.map((t) => <TaskCard key={t.id} task={t} onUpdate={(c) => update(t, c)} />)}
      </div>
    </div>
  );
}

function TaskCard({ task: t, onUpdate }: { task: Task; onUpdate: (c: Partial<Task>) => void }) {
  const [note, setNote] = useState(t.note);
  const urgent = t.priority === "urgent";
  return (
    <Card className={"p-4 " + (urgent && t.status !== "done" ? "border-red-300" : "")}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {urgent && <Badge className="border-red-200 bg-red-50 text-red-700"><AlertTriangle className="h-3 w-3" />Urgent</Badge>}
        <Badge>{KIND[t.kind] ?? t.kind}</Badge>
        <span className="text-muted-foreground" title={when(t.created_at)}>{ago(t.created_at)}</span>
        <span className="ml-auto flex gap-1">
          {(Object.keys(STATUS) as (keyof typeof STATUS)[]).map((s) => (
            <button key={s} onClick={() => onUpdate({ status: s })}
                    className={"rounded-full px-2.5 py-1 text-xs font-medium " + (t.status === s ? "bg-primary text-white" : "bg-muted text-muted-foreground hover:bg-slate-200")}>
              {STATUS[s]}
            </button>
          ))}
        </span>
      </div>
      <p className="mt-2 whitespace-pre-wrap text-sm">{t.summary}</p>
      {(t.patient_name || t.contact) && (
        <p className="mt-1 text-sm text-muted-foreground">{[t.patient_name, t.contact].filter(Boolean).join(" · ")}</p>
      )}
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <Textarea className="min-h-9 flex-1" rows={1} placeholder="Internal note (e.g. called back, left voicemail)" value={note}
                  onChange={(e) => setNote(e.target.value)} aria-label="Note" />
        {note !== t.note && <Button size="sm" variant="outline" onClick={() => onUpdate({ note })}>Save note</Button>}
        {t.session_id && (
          <Button size="sm" variant="outline" asChild>
            <a href={`#/conversations/${t.session_id}`}><MessagesSquare className="h-4 w-4" />Open conversation</a>
          </Button>
        )}
      </div>
    </Card>
  );
}
