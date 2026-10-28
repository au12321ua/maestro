import type { TaskBlocker } from "./types";

export type Tone = "neutral" | "info" | "good" | "warn" | "bad" | "muted";

export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
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

type Labels = Record<string, [string, Tone]>;

export const RUN_TERMINAL = new Set(["finished", "cancelled", "interrupted"]);

export const RUN_STATE: Labels = {
  queued: ["排队中", "info"],
  preparing: ["准备中", "info"],
  running: ["运行中", "info"],
  finalizing: ["收尾中", "info"],
  finished: ["已结束", "neutral"],
  cancelled: ["已取消", "warn"],
  interrupted: ["已中断", "bad"],
};

export const RESULT_COMPLETENESS: Labels = {
  none: ["无评分结果", "muted"],
  partial: ["部分评分", "warn"],
  complete: ["全部评分", "good"],
};

export const ARTIFACT_STATE: Labels = {
  pending: ["待保存", "muted"],
  saving: ["保存中", "info"],
  complete: ["已保存", "good"],
  partial: ["部分保存", "warn"],
  failed: ["保存失败", "bad"],
  expired: ["已过期", "warn"],
};

export const CLEANUP_STATE: Labels = {
  pending: ["待清理", "muted"],
  running: ["清理中", "info"],
  complete: ["已清理", "good"],
  failed: ["清理失败", "bad"],
  not_required: ["无需清理", "neutral"],
};

export const TRIAL_STATE: Labels = {
  queued: ["排队", "muted"],
  preparing: ["准备", "info"],
  executing: ["执行", "info"],
  verifying: ["验证", "info"],
  scored: ["已评分", "good"],
  execution_error: ["执行错误", "bad"],
  not_started: ["未开始", "muted"],
  cancelled: ["已取消", "warn"],
  interrupted: ["已中断", "bad"],
};

export const ATTEMPT_STATE: Labels = {
  starting: ["启动", "info"],
  preparing: ["准备", "info"],
  executing: ["执行", "info"],
  verifying: ["验证", "info"],
  completed: ["完成", "good"],
  failed: ["失败", "bad"],
  cancelled: ["已取消", "warn"],
  interrupted: ["已中断", "bad"],
};

/** Attempt steps as reported by the executor (Harbor: environment, agent, collecting, verifying). */
export const STEP_LABEL: Record<string, string> = {
  starting: "启动",
  environment: "准备环境",
  installing: "安装附件",
  agent: "Agent 执行",
  collecting: "收集产物",
  verifying: "验证",
};

export const QUEUE_REASON: Record<string, string> = {
  CAPACITY_SLOTS: "等待空闲试次槽位",
  CAPACITY_CPU: "等待空闲 CPU",
  CAPACITY_MEMORY: "等待空闲内存",
  PROVIDER_LIMIT: "等待模型提供商并发额度",
  STORAGE_LOW: "存储空间不足，暂停启动",
};

export function fmtDuration(ms: number): string {
  if (Number.isNaN(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} 分 ${Math.round((ms % 60_000) / 1000)} 秒`;
}

export const INVITATION_STATE: Labels = {
  pending: ["待使用", "info"],
  used: ["已使用", "neutral"],
  revoked: ["已撤销", "muted"],
  expired: ["已过期", "muted"],
};

export const PAT_STATE: Labels = {
  active: ["有效", "good"],
  revoked: ["已撤销", "muted"],
  expired: ["已过期", "muted"],
};

export function label(map: Labels, value: string | null | undefined): [string, Tone] {
  if (!value) return ["—", "muted"];
  return map[value] ?? [value, "neutral"];
}

const BLOCKER: Record<string, string> = {
  TASK_NOT_ADMITTED: "首次准备未成功，管理员处理后才能选择",
  TASK_ADMISSION_REVOKED: "准入已被管理员撤销",
  RESOURCE_CONTENT_UNAVAILABLE: "固定内容已不可得",
  CONFIG_REQUIRED_MISSING: "缺少必填作者参数",
  AUTHOR_PARAMETERS_UNRESOLVED: "作者参数无法确定",
  TEAM_LIMIT_EXCEEDED: "作者要求超过团队上限",
};

/** Run-configuration blockers and where values came from (03). */
export const CONFIG_BLOCKER: Record<string, string> = {
  LIMIT_EXCEEDED: "超过团队上限",
  BELOW_AUTHOR_RESOURCES: "不能低于任务作者给的资源",
  HARNESS_PROTOCOL_UNSUPPORTED: "Harness 不支持该模型的调用协议",
  HARNESS_MODEL_PARAMS_UNSUPPORTED: "Harness 不能传递这个模型配置里的部分参数",
  EXECUTOR_UNSUPPORTED: "执行器不支持该 Harness 与协议的组合",
  CREDENTIAL_UNAVAILABLE: "凭据不可用，需要管理员处理",
  DISABLED: "已停用",
  DELETED: "已删除",
  NOT_FOUND: "不存在",
  UNKNOWN_PARAMETER: "没有这个参数",
  SENSITIVE_PARAMETER_LITERAL: "秘密参数只能引用凭据，不能直接填值",
  RESERVED_NAME: "平台保留的名字",
  TASKS_NOT_SUBMITTABLE: "所选任务中有不存在或被阻止的",
  NO_SUBMITTABLE_TASKS: "这个版本没有可以提交的任务",
  RUN_TOO_LARGE: "任务数或试次数超过上限",
  CAPACITY_SLOTS: "并发超过主机试次槽位，永远无法开始",
  CAPACITY_CPU: "CPU 需求超过主机总量，永远无法开始",
  CAPACITY_MEMORY: "内存需求超过主机总量，永远无法开始",
  PROVIDER_LIMIT: "并发超过该提供商的上限，永远无法开始",
  ATTACHMENT_INCOMPATIBLE: "附件不支持所选 Harness",
  ATTACHMENT_CONFLICT: "两个附件提供了同名的工具、环境变量或包",
};

export const ATTACHMENT_EFFECT: Record<string, string> = {
  model_call: "模型调用",
  agent_tools: "Agent 工具",
  task_environment: "任务环境",
  verifier: "验证器",
  scoring: "评分",
};

export const SOURCE_LAYER: Record<string, string> = {
  author: "作者",
  team: "团队默认",
  plan: "实验方案",
  override: "本次填写",
  harness: "Harness 默认",
};

export function blockerText(blocker: TaskBlocker): string {
  const base = BLOCKER[blocker.code] ?? blocker.code;
  const details: string[] = [];
  const error = blocker.first_preparation_error;
  if (error?.message) details.push(String(error.message));
  if (blocker.expected_digest)
    details.push(`${String(blocker.resource_kind)} ${shortId(String(blocker.expected_digest), 19)}`);
  if (Array.isArray(blocker.parameters)) details.push(blocker.parameters.join(", "));
  return details.length ? `${base}（${details.join("；")}）` : base;
}

export const CACHE_STATE: Labels = {
  ready: ["缓存就绪", "good"],
  restoring: ["恢复中", "info"],
  stale: ["运行时恢复", "warn"],
  absent: ["未缓存", "muted"],
};

export const OPERATION_STATUS: Labels = {
  accepted: ["已受理", "info"],
  running: ["进行中", "info"],
  succeeded: ["成功", "good"],
  failed: ["失败", "bad"],
  cancelled: ["已取消", "warn"],
  interrupted: ["已中断", "bad"],
};

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

export const ARTIFACT_STATUS: Labels = {
  pending: ["待保存", "muted"],
  available: ["可下载", "good"],
  failed: ["保存失败", "bad"],
  deleting: ["回收中", "warn"],
  expired: ["已过期", "warn"],
  unavailable: ["不可用", "bad"],
  deleted: ["已删除", "muted"],
};

export const ARTIFACT_REASON: Record<string, string> = {
  RETENTION_EXPIRED: "超过保留期，内容已回收",
  STORAGE_CONTENT_MISSING: "存储中找不到内容",
  DELETED: "管理员已删除",
};

export const EXPORT_STATE: Labels = {
  queued: ["排队中", "info"],
  building: ["打包中", "info"],
  ready: ["可下载", "good"],
  failed: ["失败", "bad"],
  expired: ["已过期", "muted"],
};

export const RECYCLE_TYPE: Record<string, string> = {
  run: "运行",
  artifact: "产物",
  dataset: "数据集",
  dataset_version: "数据集版本",
  credential: "凭据",
  user: "成员",
  pat: "访问令牌",
  model_config: "模型配置",
  harness_version: "Harness 版本",
  attachment_version: "附件",
  experiment_plan: "实验方案",
};

export const STORAGE_LEVEL: Labels = {
  ok: ["存储充足", "good"],
  accept_paused: ["存储偏低：暂停受理新运行", "warn"],
  dispatch_paused: ["存储不足：暂停启动排队运行", "bad"],
};
