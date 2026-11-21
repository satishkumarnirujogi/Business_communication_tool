import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";

/** Load data, refresh on demand (and optionally every `everyMs`), surface readable errors, sign out on 401. */
export function useApi<T>(load: () => Promise<T>, deps: unknown[], onAuthError: () => void, everyMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const loader = useRef(load);
  loader.current = load;

  const refresh = useCallback(async () => {
    try {
      setData(await loader.current());
      setError("");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onAuthError();
      else setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [onAuthError]);

  useEffect(() => {
    setLoading(true);
    refresh();
    if (!everyMs) return;
    const t = setInterval(refresh, everyMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refresh, setError, setData };
}

/** Run an action; returns its result or undefined after reporting the error. */
export async function attempt<T>(fn: () => Promise<T>, report: (msg: string) => void, onAuthError: () => void) {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) onAuthError();
    else report((e as Error).message);
    return undefined;
  }
}
