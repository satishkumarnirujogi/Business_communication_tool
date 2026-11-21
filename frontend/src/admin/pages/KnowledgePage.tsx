import { CheckCircle2, FileText, Globe, Loader2, Search, Trash2, Upload, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Empty, Input, Notice, PageHeader, Select, td, th } from "@/components/ui/form";
import { api, del, post, type KbDocument, type Me } from "@/lib/api";
import { when } from "../shared";
import { attempt, useApi } from "../useApi";

interface Hit { source: string; page: number | null; category: string; similarity: number | null; keyword_coverage: number; text: string }

export default function KnowledgePage({ me, onAuthError }: { me: Me; onAuthError: () => void }) {
  const docs = useApi(() => api<{ categories: string[]; documents: KbDocument[] }>("/api/admin/knowledge"), [], onAuthError);
  const processing = (docs.data?.documents ?? []).some((d) => d.status === "processing");
  const [category, setCategory] = useState("faq");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ found: boolean; hits: Hit[] } | null>(null);
  const admin = me.role === "admin";
  const cats = docs.data?.categories ?? ["faq"];

  // While something is processing, refresh every 3 seconds.
  const refreshDocs = docs.refresh;
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(refreshDocs, 3000);
    return () => clearInterval(timer);
  }, [processing, refreshDocs]);

  async function uploadFiles(files: FileList | null) {
    if (!files) return;
    setBusy(true);
    for (const file of Array.from(files)) {
      const form = new FormData();
      form.append("file", file);
      form.append("category", category);
      const r = await attempt(() => post<{ unchanged: boolean }>("/api/admin/knowledge", form), setError, onAuthError);
      if (r) setNotice(r.unchanged ? `${file.name} is unchanged.` : `${file.name} uploaded. It's being processed.`);
    }
    setBusy(false);
    docs.refresh();
  }

  async function addUrl() {
    setBusy(true);
    const r = await attempt(() => post("/api/admin/knowledge/url", { url, category }), setError, onAuthError);
    setBusy(false);
    if (r) { setUrl(""); setNotice("Page added. It's being processed."); docs.refresh(); }
  }

  async function remove(d: KbDocument) {
    if (!confirm(`Delete "${d.name}" from the knowledge base? The assistant will no longer use it.`)) return;
    if (await attempt(() => del(`/api/admin/knowledge/${d.id}`), setError, onAuthError)) docs.refresh();
  }

  async function search(e: React.FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    setHits((await attempt(() => post<{ found: boolean; hits: Hit[] }>("/api/admin/knowledge/search", { query: q }), setError, onAuthError)) ?? null);
  }

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="Knowledge base" description="Documents the assistant answers from, always naming the source. Re-uploading a file with the same name replaces it." />
      {notice && <div className="mb-3"><Notice tone="ok" onClose={() => setNotice("")}>{notice}</Notice></div>}
      {(error || docs.error) && <div className="mb-3"><Notice tone="error" onClose={() => setError("")}>{error || docs.error}</Notice></div>}

      {admin && (
        <Card className="mb-5 grid gap-3 p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span>Category</span>
            <Select className="w-40" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
              {cats.map((c) => <option key={c}>{c}</option>)}
            </Select>
          </div>
          <label onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); uploadFiles(e.dataTransfer.files); }}
                 className="flex cursor-pointer flex-col items-center gap-1 rounded-lg border-2 border-dashed border-border px-4 py-6 text-center text-sm hover:border-primary">
            {busy ? <Loader2 className="h-5 w-5 animate-spin text-primary" /> : <Upload className="h-5 w-5 text-primary" />}
            <span className="font-medium">Drop files here or click to choose</span>
            <span className="text-xs text-muted-foreground">PDF, DOCX, TXT, MD or CSV, up to 10 MB each</span>
            <input type="file" multiple accept=".pdf,.docx,.txt,.md,.markdown,.csv" className="hidden" onChange={(e) => uploadFiles(e.target.files)} />
          </label>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (url.trim()) addUrl(); }}>
            <div className="relative flex-1"><Globe className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input className="pl-8" type="url" placeholder="https://your-clinic.example/faq" value={url} onChange={(e) => setUrl(e.target.value)} aria-label="Web page address" /></div>
            <Button type="submit" variant="outline" disabled={busy || !url.trim()}>Add web page</Button>
          </form>
        </Card>
      )}

      {docs.data && docs.data.documents.length === 0 && <Empty>No documents yet.</Empty>}
      {docs.data && docs.data.documents.length > 0 && (
        <Card className="mb-6 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-border"><tr><th className={th}>Document</th><th className={th}>Category</th><th className={th}>Status</th><th className={th}>Updated</th><th className={th} /></tr></thead>
            <tbody className="divide-y divide-border">
              {docs.data.documents.map((d) => (
                <tr key={d.id}>
                  <td className={td}><div className="flex items-center gap-2 font-medium">{d.source_type === "url" ? <Globe className="h-4 w-4 text-muted-foreground" /> : <FileText className="h-4 w-4 text-muted-foreground" />}<span className="break-all">{d.name}</span></div></td>
                  <td className={td}><Badge>{d.category}</Badge></td>
                  <td className={td}>
                    {d.status === "ready" && <span className="inline-flex items-center gap-1 text-emerald-700"><CheckCircle2 className="h-4 w-4" />Ready · {d.chunk_count} part{d.chunk_count === 1 ? "" : "s"}</span>}
                    {d.status === "processing" && <span className="inline-flex items-center gap-1 text-amber-700"><Loader2 className="h-4 w-4 animate-spin" />Processing</span>}
                    {d.status === "failed" && <span className="inline-flex items-start gap-1 text-red-700"><XCircle className="mt-0.5 h-4 w-4 shrink-0" />{d.error || "Failed"}</span>}
                  </td>
                  <td className={td + " whitespace-nowrap text-muted-foreground"}>{when(d.updated_at)}</td>
                  <td className={td + " text-right"}>{admin && <Button size="icon" variant="ghost" aria-label="Delete" onClick={() => remove(d)}><Trash2 className="h-4 w-4" /></Button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <h2 className="mb-2 text-sm font-semibold">Try a question</h2>
      <form onSubmit={search} className="mb-3 flex gap-2">
        <div className="relative flex-1"><Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input className="pl-8" placeholder="e.g. How much is a skin check?" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Test question" /></div>
        <Button type="submit" variant="outline">Search</Button>
      </form>
      {hits && !hits.found && <Notice tone="warn">Nothing relevant found: the assistant would say it doesn't have this information and offer a handoff.</Notice>}
      {hits?.hits.map((h, i) => (
        <Card key={i} className="mb-2 p-3 text-sm">
          <div className="mb-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{h.source}{h.page ? ` (page ${h.page})` : ""}</span><Badge>{h.category}</Badge>
            <span>similarity {h.similarity ?? "—"}</span><span>keywords {Math.round(h.keyword_coverage * 100)}%</span>
          </div>
          <p className="line-clamp-4 whitespace-pre-wrap">{h.text}</p>
        </Card>
      ))}
    </div>
  );
}
