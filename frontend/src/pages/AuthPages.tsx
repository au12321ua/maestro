import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorAlert, Loading, TextField } from "@/components/common";
import { ApiError, api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import type { InvitationInfo, SessionInfo } from "@/lib/types";
import { Trans, t } from "@/i18n";

type SignedIn = (session: SessionInfo) => void;

function AuthCard({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <div className="text-sm font-semibold tracking-wide text-primary">Maestro</div>
          <CardTitle className="text-xl">
            <h1>{title}</h1>
          </CardTitle>
          {description && <CardDescription>{description}</CardDescription>}
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </div>
  );
}

function fieldOf(error: unknown, name: string) {
  return error instanceof ApiError ? error.field(name) : undefined;
}

export function SetupPage({ onSignedIn }: { onSignedIn: SignedIn }) {
  const [form, setForm] = useState({
    setup_token: "",
    username: "",
    display_name: "",
    password: "",
  });
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [key]: e.target.value });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/setup", {
        body: {
          ...form,
          display_name: form.display_name || null,
        },
      });
      onSignedIn(res.data);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard
      title={t("auth.setup.title")}
      description={
        <Trans
          i18nKey="auth.setup.description"
          values={{ command: "maestro init" }}
          components={{ code: <code /> }}
        />
      }
    >
      <form className="grid gap-4" onSubmit={submit}>
        <TextField
          id="setup-token"
          label={t("auth.setup.token")}
          autoComplete="off"
          value={form.setup_token}
          onChange={set("setup_token")}
          error={fieldOf(error, "setup_token")}
          required
        />
        <TextField
          id="setup-username"
          label={t("auth.setup.username")}
          autoComplete="username"
          value={form.username}
          onChange={set("username")}
          error={fieldOf(error, "username")}
          required
        />
        <TextField
          id="setup-display"
          label={t("auth.displayName")}
          value={form.display_name}
          onChange={set("display_name")}
          error={fieldOf(error, "display_name")}
        />
        <TextField
          id="setup-password"
          label={t("auth.password")}
          type="password"
          autoComplete="new-password"
          value={form.password}
          onChange={set("password")}
          error={fieldOf(error, "password")}
          required
        />
        <ErrorAlert error={error} title={t("auth.setup.failed")} />
        <Button type="submit" disabled={busy}>
          {busy ? t("auth.submitting") : t("auth.setup.submit")}
        </Button>
      </form>
    </AuthCard>
  );
}

export function LoginPage({ onSignedIn }: { onSignedIn: SignedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/session", { body: { username, password } });
      onSignedIn(res.data);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard title={t("auth.login.title")} description={t("auth.login.description")}>
      <form className="grid gap-4" onSubmit={submit}>
        <TextField
          id="login-username"
          label={t("auth.username")}
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          error={fieldOf(error, "username")}
          required
        />
        <TextField
          id="login-password"
          label={t("auth.password")}
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={fieldOf(error, "password")}
          required
        />
        <ErrorAlert error={error} title={t("auth.login.failed")} />
        <Button type="submit" disabled={busy}>
          {busy ? t("auth.login.submitting") : t("auth.login.title")}
        </Button>
      </form>
    </AuthCard>
  );
}

function tokenFromLocation(hash: string, search: string): string {
  const fromHash = hash.replace(/^#/, "");
  if (fromHash) return decodeURIComponent(fromHash);
  return new URLSearchParams(search).get("token") ?? "";
}

export function InvitePage({ onSignedIn }: { onSignedIn: SignedIn }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => tokenFromLocation(location.hash, location.search));
  const [info, setInfo] = useState<InvitationInfo>();
  const [lookupError, setLookupError] = useState<unknown>();
  const [form, setForm] = useState({ username: "", display_name: "", password: "" });
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Drop the token from the address bar so it does not linger in history.
    if (location.hash || location.search) {
      window.history.replaceState(null, "", location.pathname);
    }
    if (!token) return;
    api
      .post("/invitations/lookup", { body: { token } })
      .then((res) => setInfo(res.data))
      .catch(setLookupError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!info) return;
    setBusy(true);
    setError(undefined);
    try {
      const body =
        info.kind === "invitation"
          ? {
              token,
              username: form.username,
              display_name: form.display_name || null,
              password: form.password,
            }
          : { token, password: form.password };
      const res = await api.post("/invitations/accept", { body: body });
      onSignedIn(res.data);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <AuthCard title={t("auth.invite.invalid")} description={t("auth.invite.noToken")}>
        <Link className="text-sm text-primary underline" to="/">
          {t("auth.invite.backToLogin")}
        </Link>
      </AuthCard>
    );
  }
  if (lookupError) {
    return (
      <AuthCard title={t("auth.invite.unusable")}>
        <div className="grid gap-3">
          <ErrorAlert error={lookupError} title={t("auth.invite.unavailable")} />
          <Link className="text-sm text-primary underline" to="/">
            {t("auth.invite.backToLogin")}
          </Link>
        </div>
      </AuthCard>
    );
  }
  if (!info) return <AuthCard title={t("auth.invite.checking")}>{<Loading />}</AuthCard>;

  const reset = info.kind === "password_reset";
  return (
    <AuthCard
      title={reset ? t("auth.invite.resetTitle") : t("auth.invite.acceptTitle")}
      description={
        reset ? (
          <Trans
            i18nKey="auth.invite.resetDescription"
            values={{ username: info.username ?? "—" }}
            components={{ b: <b /> }}
          />
        ) : (
          t(info.role === "admin" ? "auth.invite.invitedAdmin" : "auth.invite.invitedMember")
        )
      }
    >
      <form className="grid gap-4" onSubmit={submit}>
        <p className="text-xs text-muted-foreground">{t("auth.invite.expires", { time: fmtTime(info.expires_at) })}</p>
        {!reset && (
          <>
            <TextField
              id="invite-username"
              label={t("auth.username")}
              autoComplete="username"
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              error={fieldOf(error, "username")}
              required
            />
            <TextField
              id="invite-display"
              label={t("auth.displayName")}
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              error={fieldOf(error, "display_name")}
            />
          </>
        )}
        <TextField
          id="invite-password"
          label={reset ? t("auth.newPassword") : t("auth.password")}
          type="password"
          autoComplete="new-password"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          error={fieldOf(error, "password")}
          required
        />
        <ErrorAlert error={error} title={t("auth.invite.failed")} />
        <Button type="submit" disabled={busy}>
          {busy ? t("auth.submitting") : reset ? t("auth.invite.resetSubmit") : t("auth.invite.acceptSubmit")}
        </Button>
      </form>
    </AuthCard>
  );
}
