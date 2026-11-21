import type { DateSelectArg, EventClickArg, EventContentArg, EventInput, EventSourceFuncArg } from "@fullcalendar/core";
import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin from "@fullcalendar/interaction";
import luxonPlugin from "@fullcalendar/luxon3";
import FullCalendar from "@fullcalendar/react";
import timeGridPlugin from "@fullcalendar/timegrid";
import { AlertTriangle, CalendarOff, Clock, Info, Plus, Trash2, X } from "lucide-react";
import { DateTime } from "luxon";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Badge, Card, Input, Label, Select } from "@/components/ui/form";
import {
  api, ApiError, del, post, type AppointmentDetail, type CalendarData, type Doctor, type Service, type SlotSummary,
} from "@/lib/api";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const DAY_KEYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]; // FullCalendar order
const FALLBACK_COLOR = "#0f9d94";

type Notice = { tone: "info" | "warn" | "error"; text: string } | null;
type Draft = { startStr: string; endStr: string; allDay: boolean };

export default function CalendarPage({ onAuthError }: { onAuthError: () => void }) {
  const cal = useRef<FullCalendar>(null);
  const [doctors, setDoctors] = useState<Doctor[]>([]);
  const [services, setServices] = useState<Service[]>([]);
  const [doctorId, setDoctorId] = useState<number | null>(null);
  const [data, setData] = useState<CalendarData | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [booking, setBooking] = useState<AppointmentDetail | null>(null);

  const fail = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) return onAuthError();
    setNotice({ tone: "error", text: (err as Error).message });
  }, [onAuthError]);

  useEffect(() => {
    Promise.all([api<Doctor[]>("/api/admin/doctors"), api<Service[]>("/api/admin/services")])
      .then(([d, s]) => { setDoctors(d); setServices(s); })
      .catch(fail);
  }, [fail]);

  const doctor = doctors.find((d) => d.id === doctorId) ?? null;
  const doctorName = useMemo(() => Object.fromEntries(doctors.map((d) => [d.id, d.full_name])), [doctors]);
  const refetch = () => cal.current?.getApi().refetchEvents();

  // ---- events ------------------------------------------------------------
  const loadEvents = useCallback(
    (info: EventSourceFuncArg, ok: (e: EventInput[]) => void, bad: (e: Error) => void) => {
      const q = new URLSearchParams({ start: info.startStr, end: info.endStr });
      if (doctorId != null) q.set("doctor_id", String(doctorId));
      api<CalendarData>(`/api/admin/calendar?${q}`)
        .then((d) => {
          setData(d);
          const events: EventInput[] = d.appointments.map((a) => ({
            id: a.reference, start: a.start, end: a.end,
            title: a.patient, backgroundColor: a.color || FALLBACK_COLOR,
            extendedProps: { type: "appointment", ...a },
          }));
          if (doctorId != null) {
            for (const w of d.working) events.push({ start: w.start, end: w.end, display: "background", classNames: ["working"] });
          }
          for (const x of d.exceptions) {
            const label = x.kind === "off" ? (x.note || "Time off") + (x.doctor_id == null ? " (clinic)" : "") : x.note || "Extra hours";
            events.push({
              start: x.start_at, end: x.end_at, display: "background", title: label,
              classNames: [x.kind === "off" ? "time-off" : "extra"], extendedProps: { type: "exception", ...x },
            });
          }
          ok(events);
        })
        .catch((e) => { fail(e); bad(e); });
    },
    [doctorId, fail],
  );

  const businessHours = useMemo(() => {
    if (!data) return true;
    return DAY_KEYS.flatMap((key, dow) => {
      const h = data.opening_hours[key];
      return h ? [{ daysOfWeek: [dow], startTime: h[0], endTime: h[1] }] : [];
    });
  }, [data]);
  const [minTime, maxTime] = useMemo(() => {
    const hours = data ? Object.values(data.opening_hours).filter(Boolean) as [string, string][] : [];
    if (!hours.length) return ["07:00", "19:00"];
    return [hours.map((h) => h[0]).sort()[0], hours.map((h) => h[1]).sort().slice(-1)[0]];
  }, [data]);

  function renderEvent(arg: EventContentArg) {
    const p = arg.event.extendedProps;
    if (p.type !== "appointment") return arg.event.display === "background" ? <span className="fc-event-title">{arg.event.title}</span> : undefined;
    return (
      <div className="overflow-hidden px-1 py-0.5 leading-tight text-white">
        <div className="truncate text-[11px] opacity-90">{arg.timeText}</div>
        <div className="truncate font-medium">{p.patient}</div>
        <div className="truncate text-[11px] opacity-90">
          {p.service ?? "Appointment"}{doctorId == null ? ` · ${p.doctor}` : ""}
        </div>
      </div>
    );
  }

  async function onEventClick(arg: EventClickArg) {
    const p = arg.event.extendedProps;
    if (p.type !== "appointment") return;
    try {
      setBooking(await api<AppointmentDetail>(`/api/admin/appointments/${encodeURIComponent(p.reference)}`));
    } catch (e) { fail(e); }
  }

  function onSelect(sel: DateSelectArg) {
    cal.current?.getApi().unselect();
    if (doctorId == null) {
      setNotice({ tone: "info", text: "Choose a doctor first; then drag on the calendar to add hours or time off." });
      return;
    }
    setDraft({ startStr: sel.startStr, endStr: sel.endStr, allDay: sel.allDay });
  }

  function report(summary: SlotSummary | undefined, done: string, warnings: string[] = []) {
    const parts = [done, ...warnings];
    if (summary) parts.push(`${summary.added} bookable slot(s) added, ${summary.removed} removed.`);
    if (summary?.warning) parts.push(summary.warning);
    setNotice({ tone: summary?.warning || warnings.length ? "warn" : "info", text: parts.join(" ") });
    refetch();
  }

  // ---- side panel data ------------------------------------------------------
  const blocks = (data?.availability ?? []).filter((b) => b.doctor_id === doctorId)
    .sort((a, b) => a.weekday - b.weekday || a.start_time.localeCompare(b.start_time));
  const upcoming = (data?.exceptions ?? []).filter((x) => doctorId == null || x.doctor_id === doctorId || x.doctor_id == null);

  async function removeBlock(id: number) {
    try {
      const r = await del<{ slots: SlotSummary }>(`/api/admin/availability/${id}`);
      report(r.slots, "Weekly hours removed.");
    } catch (e) { fail(e); }
  }
  async function removeException(id: number) {
    try {
      const r = await del<{ slots: SlotSummary }>(`/api/admin/exceptions/${id}`);
      report(r.slots, "Exception removed.");
    } catch (e) { fail(e); }
  }

  const fmt = (iso: string, f = "ccc d LLL, HH:mm") => DateTime.fromISO(iso, { setZone: true }).toFormat(f);

  return (
    <div className="mx-auto grid max-w-[1400px] gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
      <div className="grid min-w-0 gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-1">
            <Label htmlFor="doctor">Doctor</Label>
            <Select id="doctor" className="w-64" value={doctorId ?? ""}
                    onChange={(e) => setDoctorId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">All doctors</option>
              {doctors.map((d) => (
                <option key={d.id} value={d.id}>{d.full_name} · {d.specialty}{d.active ? "" : " (hidden)"}</option>
              ))}
            </Select>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {services.filter((s) => s.active && (doctorId == null || s.doctor_ids.includes(doctorId))).map((s) => (
              <Badge key={s.id}>
                <span className="h-2.5 w-2.5 rounded-full" style={{ background: s.color || FALLBACK_COLOR }} />
                {s.name}
              </Badge>
            ))}
          </div>
        </div>

        {notice && (
          <div className={
            "flex items-start gap-2 rounded-lg border px-3 py-2 text-sm " +
            (notice.tone === "error" ? "border-red-200 bg-red-50 text-red-800"
              : notice.tone === "warn" ? "border-amber-200 bg-amber-50 text-amber-900" : "border-teal-200 bg-teal-50 text-teal-900")
          }>
            {notice.tone === "info" ? <Info className="mt-0.5 h-4 w-4 shrink-0" /> : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />}
            <span className="flex-1">{notice.text}</span>
            <button onClick={() => setNotice(null)} aria-label="Dismiss"><X className="h-4 w-4" /></button>
          </div>
        )}

        <Card className="p-3">
          <FullCalendar
            ref={cal}
            plugins={[timeGridPlugin, dayGridPlugin, interactionPlugin, luxonPlugin]}
            timeZone={data?.timezone ?? "UTC"}
            initialView="timeGridWeek"
            headerToolbar={{ left: "prev,next today", center: "title", right: "timeGridDay,timeGridWeek,dayGridMonth" }}
            buttonText={{ today: "Today", day: "Day", week: "Week", month: "Month" }}
            firstDay={1}
            height="auto"
            allDaySlot={false}
            nowIndicator
            slotDuration={`00:${String(data?.slot_minutes ?? 15).padStart(2, "0")}:00`}
            slotLabelInterval="01:00"
            slotMinTime={minTime}
            slotMaxTime={maxTime}
            businessHours={businessHours}
            eventTimeFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
            slotLabelFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
            selectable
            selectMirror
            events={loadEvents}
            eventContent={renderEvent}
            eventClick={onEventClick}
            select={onSelect}
            dayMaxEvents={4}
            eventDisplay="block"
          />
        </Card>
        <p className="text-xs text-muted-foreground">
          Times are clinic time ({data?.timezone ?? "…"}). Teal shading = working hours, striped = time off.
          {doctorId == null ? " Pick a doctor to drag-create hours or time off." : " Drag across the calendar to add hours or time off."}
        </p>
      </div>

      <aside className="grid content-start gap-4">
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><Clock className="h-4 w-4 text-primary" /> Weekly hours</div>
          {doctor == null ? (
            <p className="text-sm text-muted-foreground">Choose a doctor to see and edit their weekly hours.</p>
          ) : blocks.length === 0 ? (
            <p className="text-sm text-muted-foreground">No weekly hours yet. Drag on the week view to add some.</p>
          ) : (
            <ul className="grid gap-1">
              {blocks.map((b) => (
                <li key={b.id} className="flex items-center justify-between rounded-md px-2 py-1 text-sm hover:bg-muted">
                  <span><span className="inline-block w-24 text-muted-foreground">{WEEKDAYS[b.weekday]}</span>{b.start_time}–{b.end_time}</span>
                  <Button variant="ghost" size="icon" aria-label="Remove" onClick={() => removeBlock(b.id)}>
                    <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card className="p-4">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><CalendarOff className="h-4 w-4 text-primary" /> Time off & extra hours</div>
          {upcoming.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nothing in this period.</p>
          ) : (
            <ul className="grid gap-1">
              {upcoming.map((x) => (
                <li key={x.id} className="flex items-start justify-between gap-2 rounded-md px-2 py-1 text-sm hover:bg-muted">
                  <span>
                    <span className={x.kind === "off" ? "text-red-700" : "text-teal-700"}>{x.kind === "off" ? "Off" : "Extra"}</span>
                    {" · "}{x.doctor_id == null ? "Whole clinic" : doctorName[x.doctor_id]}
                    <br /><span className="text-xs text-muted-foreground">{fmt(x.start_at)} – {fmt(x.end_at)}{x.note ? ` · ${x.note}` : ""}</span>
                  </span>
                  <Button variant="ghost" size="icon" aria-label="Remove" onClick={() => removeException(x.id)}>
                    <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </aside>

      {draft && doctor && (
        <CreateDialog draft={draft} doctor={doctor} onClose={() => setDraft(null)} onDone={report} onError={fail} />
      )}
      {booking && (
        <BookingDialog booking={booking} fmt={fmt} onClose={() => setBooking(null)}
                       onCancelled={(ref) => { setBooking(null); report(undefined, `Booking ${ref} cancelled.`); }}
                       onError={fail} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
function CreateDialog({ draft, doctor, onClose, onDone, onError }: {
  draft: Draft; doctor: Doctor; onClose: () => void;
  onDone: (s: SlotSummary | undefined, msg: string, warnings?: string[]) => void; onError: (e: unknown) => void;
}) {
  const start = DateTime.fromISO(draft.startStr, { setZone: true });
  const end = DateTime.fromISO(draft.endStr, { setZone: true });
  const days: number[] = [];
  for (let d = start.startOf("day"); d < end; d = d.plus({ days: 1 })) days.push(d.weekday - 1);
  const [kind, setKind] = useState<"weekly" | "extra" | "off">(draft.allDay ? "off" : "weekly");
  const [from, setFrom] = useState(draft.allDay ? "00:00" : start.toFormat("HH:mm"));
  const [to, setTo] = useState(draft.allDay ? "00:00" : end.toFormat("HH:mm"));
  const [note, setNote] = useState("");
  const [clinicWide, setClinicWide] = useState(false);
  const [busy, setBusy] = useState(false);
  const uniqueDays = [...new Set(days)];

  async function save() {
    setBusy(true);
    try {
      if (kind === "weekly") {
        let last: SlotSummary | undefined;
        const warnings: string[] = [];
        for (const weekday of uniqueDays) {
          const r = await post<{ slots: SlotSummary; warnings: string[] }>("/api/admin/availability",
            { doctor_id: doctor.id, weekday, start_time: from, end_time: to });
          last = r.slots;
          warnings.push(...r.warnings);
        }
        onDone(last, `Weekly hours added for ${doctor.full_name}.`, [...new Set(warnings)]);
      } else {
        const s = draft.allDay ? start : start.set({ hour: +from.slice(0, 2), minute: +from.slice(3) });
        const e = draft.allDay ? end : end.set({ hour: +to.slice(0, 2), minute: +to.slice(3) });
        const r = await post<{ slots: SlotSummary }>("/api/admin/exceptions", {
          doctor_id: kind === "off" && clinicWide ? null : doctor.id, kind, start: s.toISO(), end: e.toISO(), note,
        });
        onDone(r.slots, kind === "off" ? "Time off saved." : "Extra hours saved.");
      }
      onClose();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  const range = draft.allDay
    ? `${start.toFormat("ccc d LLL")} – ${end.minus({ days: 1 }).toFormat("ccc d LLL")}`
    : `${start.toFormat("ccc d LLL")}${uniqueDays.length > 1 ? ` – ${end.toFormat("ccc d LLL")}` : ""}`;

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add to {doctor.full_name}'s calendar</DialogTitle>
          <DialogDescription>{range}</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-3 gap-1 rounded-lg bg-muted p-1 text-sm">
          {([["weekly", "Weekly hours"], ["extra", "Extra hours"], ["off", "Time off"]] as const).map(([k, label]) => (
            <button key={k} disabled={k === "weekly" && draft.allDay}
                    className={"rounded-md px-2 py-1.5 font-medium disabled:opacity-40 " + (kind === k ? "bg-white shadow-sm" : "text-muted-foreground")}
                    onClick={() => setKind(k)}>{label}</button>
          ))}
        </div>
        <p className="text-sm text-muted-foreground">
          {kind === "weekly" && `Repeats every ${uniqueDays.map((d) => WEEKDAYS[d]).join(", ")}.`}
          {kind === "extra" && "One-off extra working time on these dates (within opening hours)."}
          {kind === "off" && "Holiday, sick leave or blocked time. Existing bookings are kept and listed so you can rebook them."}
        </p>
        {!draft.allDay && (
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1"><Label htmlFor="from">From</Label><Input id="from" type="time" step={900} value={from} onChange={(e) => setFrom(e.target.value)} /></div>
            <div className="grid gap-1"><Label htmlFor="to">To</Label><Input id="to" type="time" step={900} value={to} onChange={(e) => setTo(e.target.value)} /></div>
          </div>
        )}
        {kind !== "weekly" && (
          <div className="grid gap-1"><Label htmlFor="note">Note</Label>
            <Input id="note" placeholder={kind === "off" ? "e.g. Sick day, Conference" : "e.g. Extra clinic"} value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} /></div>
        )}
        {kind === "off" && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={clinicWide} onChange={(e) => setClinicWide(e.target.checked)} /> Close the whole clinic (all doctors)
          </label>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={busy}><Plus className="h-4 w-4" />{busy ? "Saving…" : "Save"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BookingDialog({ booking, fmt, onClose, onCancelled, onError }: {
  booking: AppointmentDetail; fmt: (iso: string, f?: string) => string; onClose: () => void;
  onCancelled: (ref: string) => void; onError: (e: unknown) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function cancel() {
    setBusy(true);
    try {
      await post(`/api/admin/appointments/${encodeURIComponent(booking.reference)}/cancel`, { note });
      onCancelled(booking.reference);
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  const rows: [string, string][] = [
    ["When", `${fmt(booking.start, "cccc d LLLL yyyy, HH:mm")}–${fmt(booking.end, "HH:mm")}`],
    ["Doctor", booking.doctor],
    ["Service", booking.service ?? "—"],
    ["Patient", booking.patient],
    ["Email", booking.email],
    ["Phone", booking.phone],
    ["Reason", booking.reason || "—"],
    ["Status", booking.status],
  ];
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <span className="h-3 w-3 rounded-full" style={{ background: booking.color || FALLBACK_COLOR }} />
            Booking {booking.reference}
          </DialogTitle>
          <DialogDescription>Booked {fmt(booking.created_at, "d LLL yyyy, HH:mm")}</DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[90px_1fr] gap-x-3 gap-y-1.5 text-sm">
          {rows.map(([k, v]) => (<Fragment key={k}><dt className="text-muted-foreground">{k}</dt><dd className="break-words">{v}</dd></Fragment>))}
        </dl>
        {booking.status === "booked" && (confirming ? (
          <div className="grid gap-2 rounded-lg border border-red-200 bg-red-50 p-3">
            <Label htmlFor="cnote">Reason for cancelling (saved in the audit log)</Label>
            <Input id="cnote" value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Patient called" />
            <p className="text-xs text-red-800">Staff cancellations skip the 48-hour rule. Please let the patient know.</p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setConfirming(false)}>Keep booking</Button>
              <Button variant="destructive" size="sm" onClick={cancel} disabled={busy}>{busy ? "Cancelling…" : "Cancel booking"}</Button>
            </div>
          </div>
        ) : (
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>Close</Button>
            <Button variant="destructive" onClick={() => setConfirming(true)}>Cancel booking…</Button>
          </DialogFooter>
        ))}
      </DialogContent>
    </Dialog>
  );
}
