import * as React from "react";
import { cn } from "@/lib/utils";

const field =
  "h-9 w-full rounded-lg border border-border bg-white px-3 text-sm outline-none " +
  "focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-50";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => <input ref={ref} className={cn(field, className)} {...props} />,
);
Input.displayName = "Input";

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => <textarea ref={ref} className={cn(field, "h-auto min-h-20 py-2", className)} {...props} />,
);
Textarea.displayName = "Textarea";

export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => <select ref={ref} className={cn(field, "pr-8", className)} {...props} />,
);
Select.displayName = "Select";

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("text-xs font-medium text-muted-foreground", className)} {...props} />;
}

export function Field({ label, hint, children, className }: { label: string; hint?: string; children: React.ReactNode; className?: string }) {
  const id = React.useId();
  const child = React.isValidElement(children) ? React.cloneElement(children as React.ReactElement<{ id?: string }>, { id }) : children;
  return (
    <div className={cn("grid gap-1.5", className)}>
      <Label htmlFor={id}>{label}</Label>
      {child}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-border bg-white shadow-sm", className)} {...props} />;
}

export function Badge({ className, style, ...props }: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 rounded-full border border-border bg-white px-2 py-0.5 text-xs", className)}
      style={style}
      {...props}
    />
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: React.ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {description && <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function Notice({ tone = "info", children, onClose }: { tone?: "info" | "warn" | "error" | "ok"; children: React.ReactNode; onClose?: () => void }) {
  const tones = {
    info: "border-teal-200 bg-teal-50 text-teal-900",
    ok: "border-emerald-200 bg-emerald-50 text-emerald-900",
    warn: "border-amber-200 bg-amber-50 text-amber-900",
    error: "border-red-200 bg-red-50 text-red-800",
  };
  return (
    <div role={tone === "error" ? "alert" : "status"} className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-sm", tones[tone])}>
      <div className="flex-1">{children}</div>
      {onClose && <button onClick={onClose} aria-label="Dismiss" className="text-current/70">×</button>}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">{children}</div>;
}

export const th = "px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground";
export const td = "px-3 py-2 align-top";
