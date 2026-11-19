import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { Capabilities, Me } from "./types";

export interface SessionState {
  me: Me;
  capabilities: Capabilities | null;
  signOut: () => Promise<void>;
  refreshMe: () => Promise<void>;
}

export const SessionContext = createContext<SessionState | null>(null);

export function useSession(): SessionState {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside a signed-in shell");
  return value;
}

export function limit(capabilities: Capabilities | null, key: string, fallback: number): number {
  const value = capabilities?.limits?.[key];
  return typeof value === "number" ? value : fallback;
}

export interface Loaded<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Loads once on mount and whenever `deps` change; `reload` refetches without clearing data. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[] = []): Loaded<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const mine = ++generation.current;
    setLoading(true);
    try {
      const value = await loadRef.current();
      if (mine === generation.current) {
        setData(value);
        setError(undefined);
      }
    } catch (err) {
      if (mine === generation.current) setError(err);
    } finally {
      if (mine === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, reload };
}

type Page<T> = { data: T[]; meta: { next_cursor?: string | null } };

/** Load one directory page at a time and discard responses from an older scope or search. */
export function useCursorList<T>(fetchPage: (cursor?: string) => Promise<Page<T>>, key: string) {
  const fetchRef = useRef(fetchPage);
  const generation = useRef(0);
  const [state, setState] = useState<{
    key: string;
    data?: T[];
    cursor?: string | null;
    loading: boolean;
    error?: unknown;
  }>({ key, loading: true });
  useEffect(() => {
    fetchRef.current = fetchPage;
  });
  const load = useCallback(async (cursor?: string) => {
    const request = ++generation.current;
    setState((prev) => ({
      ...(prev.key === key ? prev : { key }),
      loading: true,
      error: undefined,
    }));
    try {
      const page = await fetchRef.current(cursor);
      if (request !== generation.current) return;
      setState((prev) => ({
        key,
        data: cursor ? [...(prev.data ?? []), ...page.data] : page.data,
        cursor: page.meta.next_cursor,
        loading: false,
      }));
    } catch (error) {
      if (request === generation.current) setState((prev) => ({ ...prev, loading: false, error }));
    }
  }, [key]);
  const reload = useCallback(() => load(), [load]);
  useEffect(() => {
    void reload();
    return () => { ++generation.current; };
  }, [reload]);
  const current = state.key === key ? state : { key, loading: true };
  return {
    ...current,
    reload,
    more: () => {
      if (current.cursor && !current.loading) return load(current.cursor);
    },
  };
}

/** Calls `tick` every `ms` while `active` is true. */
export function useInterval(tick: () => void, ms: number, active: boolean) {
  const ref = useRef(tick);
  useEffect(() => {
    ref.current = tick;
  });
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(id);
  }, [ms, active]);
}
