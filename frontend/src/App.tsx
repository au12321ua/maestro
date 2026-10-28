import { useCallback, useEffect, useMemo, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AlertTriangle, LogOut } from "lucide-react";
import { Toaster, toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ErrorAlert, Loading, Pill } from "@/components/common";
import { Sidebar } from "@/components/Sidebar";
import { ApiError, api, errorText, setCsrfToken, setUnauthorizedHandler } from "@/lib/api";
import { SessionContext, useInterval, useSession, type SessionState } from "@/lib/hooks";
import type { Tone } from "@/lib/format";
import type { Capabilities, Me, SessionInfo, SystemStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { InvitePage, LoginPage, SetupPage } from "@/pages/AuthPages";
import { RunListPage } from "@/pages/RunListPage";
import { NewRunPage } from "@/pages/NewRunPage";
import { RunDetailPage } from "@/pages/RunDetailPage";
import { DatasetsPage } from "@/pages/DatasetsPage";
import { AttachmentsPage } from "@/pages/AttachmentsPage";
import { PlansPage } from "@/pages/PlansPage";
import { BatchListPage, BatchPage } from "@/pages/BatchPage";
import { ExportsPage } from "@/pages/ExportsPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { AdminPage } from "@/pages/AdminPage";
import { WorkbenchPage } from "@/pages/WorkbenchPage";
import { ComparePage } from "@/pages/ComparePage";
import { CollectionsPage } from "@/pages/CollectionsPage";
import { CollectionPage } from "@/pages/CollectionPage";
import { SnapshotPage } from "@/pages/SnapshotPage";

type Boot =
  | { phase: "loading" }
  | { phase: "error"; error: unknown }
  | { phase: "setup" }
  | { phase: "signed-out" }
  | { phase: "signed-in"; me: Me };

function useBoot() {
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });

  const load = useCallback(async () => {
    try {
      const setup = await api.get("/setup");
      if (setup.data.needs_setup) {
        setBoot({ phase: "setup" });
        return;
      }
      try {
        const session = await api.get("/session");
        setCsrfToken(session.data.csrf_token);
        setBoot({ phase: "signed-in", me: session.data.user });
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) setBoot({ phase: "signed-out" });
        else throw err;
      }
    } catch (err) {
      setBoot({ phase: "error", error: err });
    }
  }, []);

  useEffect(() => {
    void load();
    setUnauthorizedHandler(() => {
      setCsrfToken(null);
      setBoot((current) => {
        if (current.phase === "signed-in") toast.error("登录已失效，请重新登录");
        return current.phase === "signed-in" ? { phase: "signed-out" } : current;
      });
    });
    return () => setUnauthorizedHandler(null);
  }, [load]);

  const signedIn = useCallback((session: SessionInfo) => {
    setCsrfToken(session.csrf_token);
    setBoot({ phase: "signed-in", me: session.user });
  }, []);

  return { boot, setBoot, signedIn, reload: load };
}

export default function App() {
  return (
    <BrowserRouter>
      <Toaster richColors position="top-right" />
      <Root />
    </BrowserRouter>
  );
}

function Root() {
  const { boot, setBoot, signedIn, reload } = useBoot();
  const location = useLocation();

  if (location.pathname === "/invite") return <InvitePage onSignedIn={signedIn} />;

  switch (boot.phase) {
    case "loading":
      return (
        <div className="p-8">
          <Loading />
        </div>
      );
    case "error":
      return (
        <div className="mx-auto max-w-xl p-8">
          <ErrorAlert error={boot.error} title="无法连接 Maestro 服务" />
          <Button className="mt-4" variant="outline" onClick={() => void reload()}>
            重试
          </Button>
        </div>
      );
    case "setup":
      return <SetupPage onSignedIn={signedIn} />;
    case "signed-out":
      return <LoginPage onSignedIn={signedIn} />;
    case "signed-in":
      return (
        <SignedIn
          me={boot.me}
          onMe={(me) => setBoot({ phase: "signed-in", me })}
          onSignedOut={() => setBoot({ phase: "signed-out" })}
        />
      );
  }
}

function SignedIn({
  me,
  onMe,
  onSignedOut,
}: {
  me: Me;
  onMe: (me: Me) => void;
  onSignedOut: () => void;
}) {
  // undefined while loading; null when unavailable (pages fall back to built-in limits).
  const [capabilities, setCapabilities] = useState<Capabilities | null | undefined>(undefined);

  useEffect(() => {
    api
      .get("/capabilities")
      .then((res) => setCapabilities(res.data))
      .catch(() => setCapabilities(null));
  }, []);

  const session = useMemo<SessionState>(
    () => ({
      me,
      capabilities: capabilities ?? null,
      signOut: async () => {
        try {
          await api.del("/session");
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 401)) {
            toast.error(`退出失败：${errorText(err)}`);
            return;
          }
        }
        setCsrfToken(null);
        onSignedOut();
      },
      refreshMe: async () => {
        const res = await api.get("/me");
        onMe(res.data);
      },
    }),
    [me, capabilities, onMe, onSignedOut],
  );

  if (capabilities === undefined) {
    return (
      <div className="p-8">
        <Loading />
      </div>
    );
  }

  return (
    <SessionContext.Provider value={session}>
      <Shell>
        <Routes>
          <Route path="/" element={<WorkbenchPage />} />
          <Route path="/runs" element={<RunListPage />} />
          <Route path="/runs/new" element={<NewRunPage />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="/datasets" element={<DatasetsPage />} />
          <Route path="/attachments" element={<AttachmentsPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/batches" element={<BatchListPage />} />
          <Route path="/batches/:batchId" element={<BatchPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/collections" element={<CollectionsPage />} />
          <Route path="/collections/:collectionId" element={<CollectionPage />} />
          <Route path="/snapshots/:snapshotId" element={<SnapshotPage />} />
          <Route path="/exports" element={<ExportsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route
            path="/admin/:section?"
            element={me.role === "admin" ? <AdminPage /> : <Navigate to="/" replace />}
          />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Shell>
    </SessionContext.Provider>
  );
}

function NotFound() {
  return <ErrorAlert error={new Error("页面不存在")} title="404" />;
}

function Shell({ children }: { children: React.ReactNode }) {
  const { me, signOut } = useSession();
  const [health, setHealth] = useState<SystemStatus | null>(null);
  const [healthError, setHealthError] = useState<unknown>();
  const pollHealth = useCallback(async () => {
    try {
      const res = await api.get("/system-status");
      setHealth(res.data);
      setHealthError(undefined);
    } catch (err) {
      setHealthError(err);
    }
  }, []);
  useEffect(() => {
    void pollHealth();
  }, [pollHealth]);
  useInterval(() => void pollHealth(), 30000, true);
  const sidebar = (
    <Sidebar
      footer={
        <>
          <div>
            <SystemIndicator health={health} error={healthError} />
          </div>
          <div className="flex min-w-0 items-center gap-2 text-sm">
            <span className="min-w-0 truncate" title={me.id}>
              {me.display_name || me.username}
              <span className="ml-1 opacity-70">({me.role === "admin" ? "管理员" : "成员"})</span>
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-sidebar-foreground hover:bg-sidebar-accent hover:text-white"
              onClick={() => void signOut()}
            >
              <LogOut />
              退出
            </Button>
          </div>
        </>
      }
      collapsedFooter={<SystemIndicator health={health} error={healthError} compact />}
    />
  );
  return (
    <div className="min-h-screen md:flex">
      {sidebar}
      <div className="flex min-w-0 flex-1 flex-col">
        <SystemNotice health={health} />
        <main className="mx-auto w-full min-w-0 max-w-[100rem] px-4 py-6 md:px-6">{children}</main>
      </div>
    </div>
  );
}

function SystemIndicator({
  health,
  error,
  compact = false,
}: {
  health: SystemStatus | null;
  error: unknown;
  compact?: boolean;
}) {
  const [tone, text]: [Tone, string] | [null, null] = error
    ? ["bad", "状态未知"]
    : !health
      ? [null, null]
      : health.status !== "ready"
        ? ["bad", "维护中"]
        : health.accepting_runs
          ? ["good", "可受理运行"]
          : ["warn", "暂不受理运行"];
  if (!tone) return null;
  const title = error ? errorText(error) : text;
  if (compact) {
    const dot = { good: "bg-emerald-400", warn: "bg-amber-400", bad: "bg-red-500" }[tone as string];
    return (
      <span title={title} aria-label={text} className={cn("mx-auto size-2.5 rounded-full", dot)} />
    );
  }
  return (
    <Pill tone={tone} title={title}>
      {text}
    </Pill>
  );
}

function SystemNotice({ health }: { health: SystemStatus | null }) {
  if (!health || (health.accepting_runs && health.status === "ready")) return null;
  const detail = health.detail;
  const reasons: string[] = [];
  if (health.status !== "ready") reasons.push(`服务状态：${health.status}`);
  if (detail.master_key_available === false) reasons.push("主密钥不可用");
  if (!detail.executor) reasons.push("未配置执行器");
  if (detail.worker_running === false) reasons.push("执行进程未运行");
  if (detail.storage.level !== "ok") reasons.push("存储空间不足");
  return (
    <div className="border-b border-amber-300 bg-amber-50 text-amber-900">
      <div className="flex items-start gap-2 px-4 py-2 text-sm md:px-6">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0">
          <b>当前无法受理新运行。</b> {reasons.join("；") || "原因未知"}。已有记录仍可查看。
          <details className="mt-1">
            <summary className="cursor-pointer text-xs">详情</summary>
            <pre className="overflow-x-auto text-xs">{JSON.stringify(detail, null, 2)}</pre>
          </details>
        </div>
      </div>
    </div>
  );
}
