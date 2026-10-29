import { language, t } from "@/i18n";
import type { TaskBlocker } from "./types";

export type Tone = "neutral" | "info" | "good" | "warn" | "bad" | "muted";

export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(language, { hour12: false });
}

export function fmtBytes(size: number | null | undefined): string {
  if (size == null) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / 1024 / 1024).toFixed(1)} MiB`;
}

export function shortId(id: string | null | undefined, length = 12): string {
  if (!id) return "—";
  return id.length > length ? `${id.slice(0, length)}…` : id;
}

/** The prefix all of ``names`` share, cut back to a separator so a word is not split. */
export function commonPrefix(names: string[]): string {
  if (names.length < 2) return "";
  let prefix = names[0];
  for (const name of names.slice(1)) {
    let i = 0;
    while (i < prefix.length && i < name.length && prefix[i] === name[i]) i++;
    prefix = prefix.slice(0, i);
    if (!prefix) return "";
  }
  const cut = Math.max(...["/", "-", "_", ".", ":"].map((sep) => prefix.lastIndexOf(sep)));
  return cut < 0 ? "" : prefix.slice(0, cut + 1);
}

/** Keep the start and the part after a long shared ``prefix``: "swebench…-11099". */
export function elideMiddle(name: string, prefix: string, keep = 8): string {
  if (prefix.length <= keep + 4 || !name.startsWith(prefix) || name.length === prefix.length) {
    return name;
  }
  const head = prefix.slice(0, keep);
  return `${head}…${name.slice(prefix.length - 1)}`;
}

type Labels = Record<string, [string, Tone]>;

/** A state's text from the locale files with its tone here; a state without text shows as is. */
function labels(texts: Record<string, string>, tones: Record<string, Tone>): Labels {
  return Object.fromEntries(
    Object.entries(tones).map(([state, tone]) => [state, [texts[state] ?? state, tone]]),
  );
}

export const RUN_TERMINAL = new Set(["finished", "cancelled", "interrupted"]);

export const RUN_STATE: Labels = labels(t("common.runState", { returnObjects: true }), {
  queued: "info",
  preparing: "info",
  running: "info",
  finalizing: "info",
  finished: "neutral",
  cancelled: "warn",
  interrupted: "bad",
});

export const RESULT_COMPLETENESS: Labels = labels(t("common.resultCompleteness", { returnObjects: true }), {
  none: "muted",
  partial: "warn",
  complete: "good",
});

export const ARTIFACT_STATE: Labels = labels(t("common.artifactState", { returnObjects: true }), {
  pending: "muted",
  saving: "info",
  complete: "good",
  partial: "warn",
  failed: "bad",
  expired: "warn",
});

export const CLEANUP_STATE: Labels = labels(t("common.cleanupState", { returnObjects: true }), {
  pending: "muted",
  running: "info",
  complete: "good",
  failed: "bad",
  not_required: "neutral",
});

export const TRIAL_STATE: Labels = labels(t("common.trialState", { returnObjects: true }), {
  queued: "muted",
  preparing: "info",
  executing: "info",
  verifying: "info",
  scored: "good",
  execution_error: "bad",
  not_started: "muted",
  cancelled: "warn",
  interrupted: "bad",
});

export const ATTEMPT_STATE: Labels = labels(t("common.attemptState", { returnObjects: true }), {
  starting: "info",
  preparing: "info",
  executing: "info",
  verifying: "info",
  completed: "good",
  failed: "bad",
  cancelled: "warn",
  interrupted: "bad",
});

/** Attempt steps as reported by the executor (Harbor: environment, agent, collecting, verifying). */
export const STEP_LABEL: Record<string, string> = t("common.stepLabel", { returnObjects: true });

export const QUEUE_REASON: Record<string, string> = t("common.queueReason", { returnObjects: true });

export function fmtDuration(ms: number): string {
  if (Number.isNaN(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return t("common.duration.minutesSeconds", {
    minutes: Math.floor(ms / 60_000),
    seconds: Math.round((ms % 60_000) / 1000),
  });
}

export const INVITATION_STATE: Labels = labels(t("common.invitationState", { returnObjects: true }), {
  pending: "info",
  used: "neutral",
  revoked: "muted",
  expired: "muted",
});

export const PAT_STATE: Labels = labels(t("common.patState", { returnObjects: true }), {
  active: "good",
  revoked: "muted",
  expired: "muted",
});

export function label(map: Labels, value: string | null | undefined): [string, Tone] {
  if (!value) return ["—", "muted"];
  return map[value] ?? [value, "neutral"];
}

const BLOCKER: Record<string, string> = t("common.blocker", { returnObjects: true });

/** Run-configuration blockers and where values came from (03). */
export const CONFIG_BLOCKER: Record<string, string> = t("common.configBlocker", { returnObjects: true });

/** A snapshot's name in lists: its label, or the start of its content digest. */
export function snapshotName(a: { label?: string | null; content_digest: string }): string {
  return a.label || a.content_digest.replace(/^sha256:/, "").slice(0, 12);
}

export const ATTACHMENT_EFFECT: Record<string, string> = t("common.attachmentEffect", { returnObjects: true });

export const SOURCE_LAYER: Record<string, string> = t("common.sourceLayer", { returnObjects: true });

export function blockerText(blocker: TaskBlocker): string {
  const base = BLOCKER[blocker.code] ?? blocker.code;
  const details: string[] = [];
  const error = blocker.first_preparation_error;
  if (error?.message) details.push(String(error.message));
  if (blocker.expected_digest)
    details.push(`${String(blocker.resource_kind)} ${shortId(String(blocker.expected_digest), 19)}`);
  if (Array.isArray(blocker.parameters)) details.push(blocker.parameters.join(", "));
  return details.length
    ? t("common.format.withDetails", { base, details: details.join(t("common.format.detailSeparator")) })
    : base;
}

export const CACHE_STATE: Labels = labels(t("common.cacheState", { returnObjects: true }), {
  ready: "good",
  restoring: "info",
  stale: "warn",
  absent: "muted",
});

export const OPERATION_STATUS: Labels = labels(t("common.operationStatus", { returnObjects: true }), {
  accepted: "info",
  running: "info",
  succeeded: "good",
  failed: "bad",
  cancelled: "warn",
  interrupted: "bad",
});

/** A statistic; null stays "—" (never 0). */
export function fmtNum(value: number | null | undefined, digits = 3): string {
  if (value == null || Number.isNaN(value)) return "—";
  return String(Number(value.toFixed(digits)));
}

export function fmtPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${Number((value * 100).toFixed(1))}%`;
}

/** Local calendar day ("YYYY-MM-DD") to the API's RFC 3339 UTC form with microseconds. */
export function dayToApiTime(day: string, plusDays = 0): string | undefined {
  if (!day) return undefined;
  const date = new Date(`${day}T00:00:00`);
  if (Number.isNaN(date.getTime())) return undefined;
  date.setDate(date.getDate() + plusDays);
  return date.toISOString().replace(/\.(\d{3})Z$/, ".$1000Z");
}

export const ARTIFACT_STATUS: Labels = labels(t("common.artifactStatus", { returnObjects: true }), {
  pending: "muted",
  available: "good",
  failed: "bad",
  deleting: "warn",
  expired: "warn",
  unavailable: "bad",
  deleted: "muted",
});

export const ARTIFACT_REASON: Record<string, string> = t("common.artifactReason", { returnObjects: true });

export const EXPORT_STATE: Labels = labels(t("common.exportState", { returnObjects: true }), {
  queued: "info",
  building: "info",
  ready: "good",
  failed: "bad",
  expired: "muted",
});

export const RECYCLE_TYPE: Record<string, string> = t("common.recycleType", { returnObjects: true });

export const STORAGE_LEVEL: Labels = labels(t("common.storageLevel", { returnObjects: true }), {
  ok: "good",
  accept_paused: "warn",
  dispatch_paused: "bad",
});
