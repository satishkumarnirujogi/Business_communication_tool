// Build an .ics file for "Add to calendar" in the browser (no patient data leaves the page).

export interface BookingCard {
  reference: string; doctor: string | null; service: string | null; start: string | null; end: string | null;
  label: string | null; clinic: string; address: string; phone: string;
}

const stamp = (iso: string) => new Date(iso).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
const esc = (s: string) => s.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/([,;])/g, "\\$1");

export function downloadIcs(b: BookingCard) {
  if (!b.start || !b.end) return;
  const title = [b.service ?? "Appointment", b.doctor].filter(Boolean).join(" – ");
  const lines = [
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Clinic Assistant//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
    "BEGIN:VEVENT",
    `UID:${b.reference}@clinic-assistant`,
    `DTSTAMP:${stamp(new Date().toISOString())}`,
    `DTSTART:${stamp(b.start)}`,
    `DTEND:${stamp(b.end)}`,
    `SUMMARY:${esc(`${title} (${b.clinic})`)}`,
    `LOCATION:${esc(b.address || b.clinic)}`,
    `DESCRIPTION:${esc(`Booking reference ${b.reference}. Questions or changes: ${b.phone}`)}`,
    "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", `DESCRIPTION:${esc(title)}`, "END:VALARM",
    "END:VEVENT", "END:VCALENDAR",
  ];
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `appointment-${b.reference}.ics`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
