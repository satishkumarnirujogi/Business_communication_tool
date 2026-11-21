import {
  BookOpen, CalendarDays, ClipboardList, Inbox, LayoutDashboard, LogOut, Menu, MessagesSquare, Settings as SettingsIcon,
  Stethoscope, Users, X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, Field, Input, Notice } from "@/components/ui/form";
import { api, ApiError, getAuth, post, setAuth, type Me } from "@/lib/api";
import AuditPage from "./pages/AuditPage";
import CalendarPage from "./pages/CalendarPage";
import ConversationsPage from "./pages/ConversationsPage";
import DirectoryPage from "./pages/DirectoryPage";
import HandoffsPage from "./pages/HandoffsPage";
import KnowledgePage from "./pages/KnowledgePage";
import OverviewPage from "./pages/OverviewPage";
import SettingsPage from "./pages/SettingsPage";
import SetupWizard from "./Setup";

export interface Clinic { name: string; demo?: boolean; branding: { logo_url: string; primary_color: string } }

const PAGES = [
  { id: "overview", label: "Overview", icon: LayoutDashboard, admin: false },
  { id: "calendar", label: "Calendar", icon: CalendarDays, admin: false },
  { id: "conversations", label: "Conversations", icon: MessagesSquare, admin: false },
  { id: "handoffs", label: "Handoff queue", icon: Inbox, admin: false },
  { id: "directory", label: "Doctors & services", icon: Users, admin: true },
  { id: "knowledge", label: "Knowledge base", icon: BookOpen, admin: false },
  { id: "audit", label: "Audit log", icon: ClipboardList, admin: false },
  { id: "settings", label: "Settings", icon: SettingsIcon, admin: true },
] as const;
type PageId = (typeof PAGES)[number]["id"];

function routeFromHash(): { page: PageId; arg: string } {
  const [page, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const known = PAGES.some((p) => p.id === page);
  return { page: (known ? page : "overview") as PageId, arg: decodeURIComponent(rest.join("/")) };
}

export function go(page: PageId, arg = "") {
  location.hash = `/${page}${arg ? `/${encodeURIComponent(arg)}` : ""}`;
}

export default function App() {
  const [clinic, setClinic] = useState<Clinic | null>(null);
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);

  const loadClinic = useCallback(() => {
    fetch("/api/clinic").then((r) => r.json()).then((c: Clinic) => {
      setClinic(c);
      document.title = `${c.name} · Admin`;
      if (c.branding?.primary_color) document.documentElement.style.setProperty("--color-primary", c.branding.primary_color);
    }).catch(() => {});
  }, []);

  const loadMe = useCallback(async () => {
    if (!getAuth()) {
      setMe(null);
      return;
    }
    try {
      setMe(await api<Me>("/api/auth/me"));
    } catch {
      setAuth(null);
      setMe(null);
    }
  }, []);

  useEffect(() => {
    loadClinic();
    fetch("/api/setup/status").then((r) => r.json()).then((s) => setNeedsSetup(!!s.needs_setup)).catch(() => setNeedsSetup(false));
    loadMe().finally(() => setChecking(false));
  }, [loadClinic, loadMe]);

  if (checking || needsSetup === null) return <div className="grid h-full place-items-center text-sm text-muted-foreground">Loading…</div>;
  if (!me && needsSetup) {
    return <SetupWizard onDone={async () => { setNeedsSetup(false); loadClinic(); await loadMe(); }} />;
  }
  if (!me) return <Login clinic={clinic} onDone={loadMe} />;
  return <Shell clinic={clinic} me={me} onClinicChange={loadClinic} onSignOut={async () => {
    try { await post("/api/auth/logout"); } catch { /* already gone */ }
    setAuth(null);
    setMe(null);
  }} />;
}

function Shell({ clinic, me, onSignOut, onClinicChange }: { clinic: Clinic | null; me: Me; onSignOut: () => void; onClinicChange: () => void }) {
  const [route, setRoute] = useState(routeFromHash());
  const [menu, setMenu] = useState(false);
  const pages = PAGES.filter((p) => !p.admin || me.role === "admin");

  useEffect(() => {
    const on = () => { setRoute(routeFromHash()); setMenu(false); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const page = pages.some((p) => p.id === route.page) ? route.page : "overview";
  const authError = () => onSignOut();

  return (
    <div className="flex h-full">
      <aside className={"fixed inset-y-0 left-0 z-40 w-64 shrink-0 border-r border-border bg-white transition-transform lg:static lg:translate-x-0 " +
        (menu ? "translate-x-0" : "-translate-x-full")}>
        <div className="flex items-center gap-3 border-b border-border px-4 py-4">
          {clinic?.branding.logo_url ? (
            <img src={clinic.branding.logo_url} alt="" className="h-9 w-9 rounded-lg object-cover" />
          ) : (
            <span className="grid h-9 w-9 place-items-center rounded-lg bg-primary-soft text-primary"><Stethoscope className="h-5 w-5" /></span>
          )}
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">{clinic?.name ?? "Clinic"}</div>
            <div className="text-xs text-muted-foreground">Admin</div>
          </div>
          <button className="ml-auto lg:hidden" onClick={() => setMenu(false)} aria-label="Close menu"><X className="h-4 w-4" /></button>
        </div>
        <nav className="grid gap-0.5 p-2">
          {pages.map((p) => (
            <a key={p.id} href={`#/${p.id}`}
               className={"flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium " +
                 (page === p.id ? "bg-primary-soft text-primary" : "text-slate-600 hover:bg-muted")}>
              <p.icon className="h-4 w-4" />{p.label}
            </a>
          ))}
        </nav>
        <div className="absolute inset-x-0 bottom-0 border-t border-border p-3">
          <div className="mb-2 px-1 text-xs text-muted-foreground">
            <div className="truncate font-medium text-foreground">{me.name}</div>
            <div className="truncate">{me.email || "Admin token"} · {me.role}</div>
          </div>
          <Button variant="outline" size="sm" className="w-full" onClick={onSignOut}><LogOut className="h-4 w-4" />Sign out</Button>
        </div>
      </aside>
      {menu && <div className="fixed inset-0 z-30 bg-slate-900/20 lg:hidden" onClick={() => setMenu(false)} />}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-border bg-white px-4 py-3 lg:hidden">
          <button onClick={() => setMenu(true)} aria-label="Open menu"><Menu className="h-5 w-5" /></button>
          <span className="text-sm font-semibold">{pages.find((p) => p.id === page)?.label}</span>
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-4 sm:p-6">
          {clinic?.demo && (
            <div className="mx-auto mb-4 max-w-7xl rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              Demo mode: this is a sample clinic with made-up doctors, patients and bookings. Set <code>DEMO_MODE=false</code> before
              using it for a real clinic.
            </div>
          )}
          {page === "overview" && <OverviewPage onAuthError={authError} />}
          {page === "calendar" && <CalendarPage onAuthError={authError} />}
          {page === "conversations" && <ConversationsPage selected={route.arg} onAuthError={authError} />}
          {page === "handoffs" && <HandoffsPage onAuthError={authError} />}
          {page === "directory" && <DirectoryPage onAuthError={authError} />}
          {page === "knowledge" && <KnowledgePage me={me} onAuthError={authError} />}
          {page === "audit" && <AuditPage onAuthError={authError} />}
          {page === "settings" && <SettingsPage me={me} onAuthError={authError} onClinicChange={onClinicChange} />}
        </main>
      </div>
    </div>
  );
}

function Login({ clinic, onDone }: { clinic: Clinic | null; onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [useToken, setUseToken] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (useToken) {
        setAuth({ kind: "admin-token", token: token.trim() });
        await api("/api/auth/me");
      } else {
        const r = await post<{ token: string }>("/api/auth/login", { email, password });
        setAuth({ kind: "session", token: r.token });
      }
      onDone();
    } catch (err) {
      setAuth(null);
      setError(err instanceof ApiError ? err.message : "Couldn't reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid h-full place-items-center p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-5 flex items-center gap-3">
          {clinic?.branding.logo_url ? (
            <img src={clinic.branding.logo_url} alt="" className="h-10 w-10 rounded-lg object-cover" />
          ) : (
            <span className="grid h-10 w-10 place-items-center rounded-lg bg-primary-soft text-primary"><Stethoscope className="h-5 w-5" /></span>
          )}
          <div>
            <div className="font-semibold">{clinic?.name ?? "Clinic"}</div>
            <div className="text-xs text-muted-foreground">Staff sign-in</div>
          </div>
        </div>
        <form onSubmit={submit} className="grid gap-3">
          {useToken ? (
            <Field label="Admin token (ADMIN_TOKEN in .env)">
              <Input type="password" value={token} onChange={(e) => setToken(e.target.value)} required autoFocus />
            </Field>
          ) : (
            <>
              <Field label="Email"><Input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></Field>
              <Field label="Password"><Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
            </>
          )}
          {error && <Notice tone="error">{error}</Notice>}
          <Button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
          <button type="button" className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                  onClick={() => { setUseToken(!useToken); setError(""); }}>
            {useToken ? "Sign in with email and password" : "Use the admin token instead (recovery)"}
          </button>
        </form>
      </Card>
    </div>
  );
}
