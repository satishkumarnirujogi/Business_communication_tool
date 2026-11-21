// Client for the FastAPI admin API. Sign-in gives a bearer session token; the break-glass ADMIN_TOKEN
// (X-Admin-Token) still works for recovery.

const KEY = "clinic-admin-auth";

export type Auth = { kind: "session" | "admin-token"; token: string } | null;

let current: Auth = null;
try {
  current = JSON.parse(sessionStorage.getItem(KEY) ?? "null");
} catch {
  current = null;
}

export function getAuth(): Auth {
  return current;
}

export function setAuth(auth: Auth) {
  current = auth;
  try {
    if (auth) sessionStorage.setItem(KEY, JSON.stringify(auth));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* private mode: stays in memory for this page */
  }
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

function authHeaders(): Record<string, string> {
  if (!current) return {};
  return current.kind === "session" ? { Authorization: `Bearer ${current.token}` } : { "X-Admin-Token": current.token };
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isForm = init.body instanceof FormData;
  const res = await fetch(path, {
    ...init,
    headers: { ...(isForm ? {} : { "Content-Type": "application/json" }), ...authHeaders(), ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(message, res.status);
  }
  return res.json() as Promise<T>;
}

const json = (method: string) => <T,>(path: string, body?: unknown) =>
  api<T>(path, { method, body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body) });
export const post = json("POST");
export const put = json("PUT");
export const patch = json("PATCH");
export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });

// ---- shared types --------------------------------------------------------------------
export interface Me { id: number | null; name: string; email: string; role: "admin" | "staff" }
export interface Doctor { id: number; full_name: string; specialty: string; languages: string; bio: string; photo_url: string; active: boolean }
export interface Service {
  id: number; name: string; duration_minutes: number; price: number | null; color: string; active: boolean;
  description: string; doctor_ids: number[];
}
export interface Appointment {
  reference: string; status: string; doctor_id: number; doctor: string; patient: string; service: string | null;
  color: string; reason: string; start: string; end: string;
}
export interface AppointmentDetail extends Appointment { email: string; phone: string; created_at: string; cancelled_at: string | null }
export interface AvailabilityBlock { id: number; doctor_id: number; weekday: number; start_time: string; end_time: string }
export interface ScheduleException {
  id: number; doctor_id: number | null; kind: "off" | "extra"; start_at: string; end_at: string; note: string;
}
export interface CalendarData {
  timezone: string;
  slot_minutes: number;
  opening_hours: Record<string, [string, string] | null>;
  appointments: Appointment[];
  working: { doctor_id: number; start: string; end: string }[];
  availability: AvailabilityBlock[];
  exceptions: ScheduleException[];
}
export interface SlotSummary { added: number; removed: number; conflicts: { reference: string }[]; warning?: string }
export interface Task {
  id: number; created_at: string; updated_at: string; session_id: string | null; kind: string; priority: "normal" | "urgent";
  summary: string; patient_name: string; contact: string; status: "open" | "in_progress" | "done"; note: string;
}
export interface Conversation {
  id: string; channel: string; language: string; status: "bot" | "human" | "closed"; patient_name: string;
  taken_over_by: string; escalated: boolean; message_count: number; started_at: string; last_message_at: string;
  last_message?: string; open_tasks?: number;
}
export interface ConversationMessage {
  id: number; created_at: string; role: "patient" | "assistant" | "staff" | "system"; author: string; content: string;
  meta: { sources?: string[]; escalation?: { category: string } | null } | null;
}
export interface AuditRow {
  id: number; created_at: string; session_id: string | null; actor: string; action: string; params: unknown;
  decision: string; rule: string | null; reason: string | null; result: unknown;
}
export interface KbDocument {
  id: number; name: string; category: string; source_type: string; url: string | null; status: string;
  error: string | null; chunk_count: number; size_bytes: number | null; updated_at: string;
}
