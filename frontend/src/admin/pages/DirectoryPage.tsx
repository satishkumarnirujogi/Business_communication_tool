import { Eye, EyeOff, Pencil, Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Card, Field, Input, Notice, PageHeader, Textarea, td, th } from "@/components/ui/form";
import { api, post, put, type Doctor, type Service } from "@/lib/api";
import { attempt, useApi } from "../useApi";

export default function DirectoryPage({ onAuthError }: { onAuthError: () => void }) {
  const doctors = useApi(() => api<Doctor[]>("/api/admin/doctors"), [], onAuthError);
  const services = useApi(() => api<Service[]>("/api/admin/services"), [], onAuthError);
  const [editDoctor, setEditDoctor] = useState<Partial<Doctor> | null>(null);
  const [editService, setEditService] = useState<Partial<Service> | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const names = Object.fromEntries((doctors.data ?? []).map((d) => [d.id, d.full_name]));

  async function saveDoctor(d: Partial<Doctor>) {
    const body = { full_name: d.full_name, specialty: d.specialty, languages: d.languages || "en", bio: d.bio || "",
                   photo_url: d.photo_url || "", active: d.active ?? true };
    const r = await attempt(() => (d.id ? put(`/api/admin/doctors/${d.id}`, body) : post("/api/admin/doctors", body)), setError, onAuthError);
    if (r) { setEditDoctor(null); doctors.refresh(); setNotice(d.id ? "Doctor saved." : "Doctor added. Give them weekly hours in the Calendar."); }
  }

  async function saveService(s: Partial<Service>) {
    const body = { name: s.name, duration_minutes: Number(s.duration_minutes), price: s.price === null || s.price === undefined || String(s.price) === "" ? null : Number(s.price),
                   description: s.description || "", color: s.color || "", active: s.active ?? true, doctor_ids: s.doctor_ids ?? [] };
    const r = await attempt(() => (s.id ? put(`/api/admin/services/${s.id}`, body) : post("/api/admin/services", body)), setError, onAuthError);
    if (r) { setEditService(null); services.refresh(); setNotice("Service saved."); }
  }

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="Doctors & services" description="What patients can book, and with whom. Hidden entries keep their history." />
      {notice && <div className="mb-3"><Notice tone="ok" onClose={() => setNotice("")}>{notice}</Notice></div>}
      {(error || doctors.error || services.error) && <div className="mb-3"><Notice tone="error" onClose={() => setError("")}>{error || doctors.error || services.error}</Notice></div>}

      <div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">Doctors</h2>
        <Button size="sm" onClick={() => setEditDoctor({ active: true, languages: "en" })}><Plus className="h-4 w-4" />Add doctor</Button></div>
      <Card className="mb-6 overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-border"><tr><th className={th}>Name</th><th className={th}>Specialty</th><th className={th}>Languages</th><th className={th}>Services</th><th className={th} /></tr></thead>
          <tbody className="divide-y divide-border">
            {(doctors.data ?? []).map((d) => (
              <tr key={d.id} className={d.active ? "" : "text-muted-foreground"}>
                <td className={td}><div className="font-medium">{d.full_name}</div>{!d.active && <span className="text-xs">Hidden</span>}</td>
                <td className={td}>{d.specialty}</td>
                <td className={td}>{d.languages}</td>
                <td className={td}>{(services.data ?? []).filter((s) => s.doctor_ids.includes(d.id) && s.active).map((s) => s.name).join(", ") || "—"}</td>
                <td className={td + " whitespace-nowrap text-right"}>
                  <Button size="icon" variant="ghost" aria-label="Edit" onClick={() => setEditDoctor(d)}><Pencil className="h-4 w-4" /></Button>
                  <Button size="icon" variant="ghost" aria-label={d.active ? "Hide" : "Show"} onClick={() => saveDoctor({ ...d, active: !d.active })}>
                    {d.active ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">Services</h2>
        <Button size="sm" onClick={() => setEditService({ active: true, duration_minutes: 30, doctor_ids: [], color: "#0f9d94" })}><Plus className="h-4 w-4" />Add service</Button></div>
      <Card className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-border"><tr><th className={th}>Service</th><th className={th}>Length</th><th className={th}>Price</th><th className={th}>Doctors</th><th className={th} /></tr></thead>
          <tbody className="divide-y divide-border">
            {(services.data ?? []).map((s) => (
              <tr key={s.id} className={s.active ? "" : "text-muted-foreground"}>
                <td className={td}><div className="flex items-center gap-2 font-medium"><span className="h-3 w-3 rounded-full" style={{ background: s.color || "#0f9d94" }} />{s.name}</div>
                  {!s.active && <span className="text-xs">Hidden</span>}</td>
                <td className={td}>{s.duration_minutes} min</td>
                <td className={td}>{s.price == null ? "On request" : s.price.toFixed(2)}</td>
                <td className={td}>{s.doctor_ids.map((id) => names[id]).filter(Boolean).join(", ") || "—"}</td>
                <td className={td + " whitespace-nowrap text-right"}>
                  <Button size="icon" variant="ghost" aria-label="Edit" onClick={() => setEditService(s)}><Pencil className="h-4 w-4" /></Button>
                  <Button size="icon" variant="ghost" aria-label={s.active ? "Hide" : "Show"} onClick={() => saveService({ ...s, active: !s.active })}>
                    {s.active ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {editDoctor && (
        <Dialog open onOpenChange={(o) => !o && setEditDoctor(null)}>
          <DialogContent className="max-w-lg">
            <DialogHeader><DialogTitle>{editDoctor.id ? "Edit doctor" : "Add doctor"}</DialogTitle></DialogHeader>
            <div className="grid gap-3">
              <Field label="Name"><Input value={editDoctor.full_name ?? ""} onChange={(e) => setEditDoctor({ ...editDoctor, full_name: e.target.value })} /></Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Specialty"><Input value={editDoctor.specialty ?? ""} onChange={(e) => setEditDoctor({ ...editDoctor, specialty: e.target.value })} /></Field>
                <Field label="Languages" hint="Codes, e.g. de, en"><Input value={editDoctor.languages ?? ""} onChange={(e) => setEditDoctor({ ...editDoctor, languages: e.target.value })} /></Field>
              </div>
              <Field label="Short bio"><Textarea value={editDoctor.bio ?? ""} onChange={(e) => setEditDoctor({ ...editDoctor, bio: e.target.value })} /></Field>
              <Field label="Photo URL (optional)"><Input value={editDoctor.photo_url ?? ""} onChange={(e) => setEditDoctor({ ...editDoctor, photo_url: e.target.value })} /></Field>
            </div>
            <DialogFooter><Button variant="outline" onClick={() => setEditDoctor(null)}>Cancel</Button><Button onClick={() => saveDoctor(editDoctor)}>Save</Button></DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {editService && (
        <Dialog open onOpenChange={(o) => !o && setEditService(null)}>
          <DialogContent className="max-w-lg">
            <DialogHeader><DialogTitle>{editService.id ? "Edit service" : "Add service"}</DialogTitle></DialogHeader>
            <div className="grid gap-3">
              <Field label="Name"><Input value={editService.name ?? ""} onChange={(e) => setEditService({ ...editService, name: e.target.value })} /></Field>
              <div className="grid grid-cols-3 gap-3">
                <Field label="Minutes"><Input type="number" min={5} step={5} value={editService.duration_minutes ?? 30} onChange={(e) => setEditService({ ...editService, duration_minutes: Number(e.target.value) })} /></Field>
                <Field label="Price" hint="Empty = on request"><Input type="number" min={0} step="0.01" value={editService.price ?? ""} onChange={(e) => setEditService({ ...editService, price: e.target.value === "" ? null : Number(e.target.value) })} /></Field>
                <Field label="Colour"><Input type="color" className="p-1" value={editService.color || "#0f9d94"} onChange={(e) => setEditService({ ...editService, color: e.target.value })} /></Field>
              </div>
              <Field label="Description"><Textarea value={editService.description ?? ""} onChange={(e) => setEditService({ ...editService, description: e.target.value })} /></Field>
              <div className="grid gap-1.5">
                <span className="text-xs font-medium text-muted-foreground">Offered by</span>
                <div className="grid grid-cols-2 gap-1 text-sm">
                  {(doctors.data ?? []).filter((d) => d.active).map((d) => (
                    <label key={d.id} className="flex items-center gap-2">
                      <input type="checkbox" checked={(editService.doctor_ids ?? []).includes(d.id)}
                             onChange={(e) => setEditService({ ...editService, doctor_ids: e.target.checked ? [...(editService.doctor_ids ?? []), d.id] : (editService.doctor_ids ?? []).filter((x) => x !== d.id) })} />
                      {d.full_name}
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <DialogFooter><Button variant="outline" onClick={() => setEditService(null)}>Cancel</Button><Button onClick={() => saveService(editService)}>Save</Button></DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
