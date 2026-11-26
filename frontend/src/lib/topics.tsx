import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/hooks";
import type { Topic } from "@/lib/types";

interface TopicBrowsing {
  topicId: string | null;
  topic: Topic | null;
  loading: boolean;
  error: unknown;
  reload: () => Promise<void>;
  choose: (id: string, reset?: string[]) => void;
}

const ScopeContext = createContext<TopicBrowsing | null>(null);

export function TopicScopeProvider({ userId, children }: { userId: string; children: ReactNode }) {
  const key = `maestro.topic.v1.${userId}`;
  const [saved, setSaved] = useState(() => {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  });
  const [params, setParams] = useSearchParams();
  const explicit = params.get("topic");
  const topicId = (explicit ?? saved) || null;

  useEffect(() => {
    if (explicit === null) return;
    setSaved(explicit || null);
    try {
      if (explicit) localStorage.setItem(key, explicit);
      else localStorage.removeItem(key);
    } catch {
      // The scope still follows this signed-in session when storage is unavailable.
    }
  }, [explicit, key]);

  const detail = useLoad(async () =>
    topicId
      ? (await api.get("/topics/{topic_id}", { path: { topic_id: topicId } })).data
      : null,
  [topicId]);

  const choose = (identity: string, reset: string[] = []) => {
    const next = new URLSearchParams(params);
    next.set("topic", identity);
    for (const field of ["dataset", "version", "snapshot", "base", "name", ...reset]) next.delete(field);
    setParams(next, { replace: true });
  };

  return (
    <ScopeContext.Provider value={{
      topicId,
      topic: detail.data?.id === topicId ? detail.data : null,
      loading: detail.loading,
      error: detail.error,
      reload: detail.reload,
      choose,
    }}>
      {children}
    </ScopeContext.Provider>
  );
}

export function useTopicScope() {
  const scope = useContext(ScopeContext);
  if (!scope) throw new Error("Topic scope is unavailable");
  return scope;
}

export function scopedPath(path: string, topicId: string | null) {
  if (!topicId) return path;
  const joiner = path.includes("?") ? "&" : "?";
  return `${path}${joiner}topic=${encodeURIComponent(topicId)}`;
}
