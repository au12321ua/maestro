import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  EmptyNote,
  ErrorAlert,
  JsonBlock,
  Loading,
  LongName,
  PageTitle,
  Pill,
  SelectField,
  TextField,
} from "@/components/common";
import { ApiError, api } from "@/lib/api";
import {
  CONFIG_BLOCKER,
  SOURCE_LAYER,
  blockerText,
  commonPrefix,
  fmtTime,
  snapshotName,
} from "@/lib/format";
import { limit, useLoad, useSession } from "@/lib/hooks";
import { DIMENSION, SCENARIO } from "@/lib/comparison";
import type {
  BatchPreview,
  ConfigRequest,
  ConfigResolution,
  ExecutionMode,
  PartialRequest,
  ParameterValue,
  RunCreate,
  Task,
  EffectiveTask,
  Dimension,
  Scenario,
} from "@/lib/types";
import { TaskStatusCells } from "@/components/TaskStatusCells";

function newKey() {
  return crypto.randomUUID();
}

const NESTED = new Set(["resources", "timeouts", "parameters", "harness_params"]);

/** The fields of a full request that differ from the plan's: the rest the plan supplies,
 *  and the resolution reports those with the plan as their source. */
function ownFields(full: ConfigRequest, template: PartialRequest): Partial<ConfigRequest> {
  const own: Record<string, unknown> = {};
  const plan = template as Record<string, unknown>;
  for (const [key, value] of Object.entries(full)) {
    if (value === null || value === undefined) continue;
    if (NESTED.has(key)) {
      const planned = (plan[key] ?? {}) as Record<string, unknown>;
      const changed = Object.fromEntries(
        Object.entries(value as Record<string, unknown>).filter(
          ([k, v]) => v !== null && JSON.stringify(v) !== JSON.stringify(planned[k]),
        ),
      );
      if (Object.keys(changed).length > 0) own[key] = changed;
    } else if (JSON.stringify(value) !== JSON.stringify(plan[key])) {
      own[key] = value;
    }
  }
  return own as Partial<ConfigRequest>;
}

/** Drops unset fields, for saving a request as a plan template. */
function setFields(full: ConfigRequest): Partial<ConfigRequest> {
  const found: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(full)) {
    if (value === null || value === undefined) continue;
    if (NESTED.has(key)) {
      const kept = Object.fromEntries(
        Object.entries(value as Record<string, unknown>).filter(([, v]) => v != null),
      );
      if (Object.keys(kept).length > 0) found[key] = kept;
    } else if (!(Array.isArray(value) && value.length === 0)) {
      found[key] = value;
    }
  }
  return found as Partial<ConfigRequest>;
}

interface DraftMember {
  name: string;
  request: Partial<ConfigRequest>;
  label: string;
}

/** Blank means "inherit": the field is sent as null so the resolver takes the layer below. */
// Which collection dimension each override field belongs to (06 section 8).
const OVERRIDE_DIMENSION: Record<string, string> = {
  repetitions: "repetitions",
  agent_sec: "resources",
  cpus: "resources",
  memory_mb: "resources",
  bootstrap_sec: "attachments",
};

function numberOrNull(text: string): number | null {
  return text.trim() === "" ? null : Number(text);
}

export function NewRunPage() {
  const { capabilities } = useSession();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // A modified run starts from that run's request; its differences are recorded.
  const sourceRunId = params.get("from");
  const plans = useLoad(async () => (await api.get("/plans")).data);
  // From a collection: its fixed fields are filled in and locked, and the run joins it.
  const collectionId = params.get("collection");
  const collection = useLoad(
    async () =>
      collectionId
        ? (await api.get("/collections/{collection_id}", { path: { collection_id: collectionId } }))
            .data
        : undefined,
    [collectionId],
  );
  const fixedDimensions = new Set(Object.keys(collection.data?.fixed ?? {}));
  const locked = (dimension: string) => !!collectionId && fixedDimensions.has(dimension);
  const datasets = useLoad(
    async () => (await api.get("/datasets", { query: { limit: 200 } })).data,
  );
  const models = useLoad(async () => (await api.get("/model-configs")).data);
  const harnesses = useLoad(async () => (await api.get("/harness-versions")).data);
  const credentials = useLoad(async () => (await api.get("/credentials")).data);
  const attachments = useLoad(
    async () => (await api.get("/attachments", { query: { include_disabled: false } })).data,
  );
  const versions = useMemo(
    () =>
      (datasets.data ?? []).flatMap((d) =>
        d.versions.map((v) => ({ dataset: d, version: v })),
      ),
    [datasets.data],
  );

  const [chosenVersion, setChosenVersion] = useState("");
  const versionId = chosenVersion || versions[0]?.version.id || "";

  const tasks = useLoad(
    async () =>
      versionId
        ? (
            await api.get("/dataset-versions/{version_id}/tasks", {
              path: { version_id: versionId },
            })
          ).data
        : ([] as Task[]),
    [versionId],
  );
  const taskPrefix = commonPrefix(tasks.data?.map((t) => t.task_key) ?? []);
  const detail = useLoad(
    async () =>
      versionId
        ? (await api.get("/dataset-versions/{version_id}", { path: { version_id: versionId } }))
            .data
        : undefined,
    [versionId],
  );

  const [name, setName] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("agent");
  const [modelId, setModelId] = useState("");
  const [harnessId, setHarnessId] = useState("");
  const [harnessParams, setHarnessParams] = useState<Record<string, string>>(
    {},
  );
  const [mode, setMode] = useState<"all_eligible" | "explicit">("all_eligible");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [overrides, setOverrides] = useState({
    repetitions: "",
    concurrency: "",
    run_deadline_hours: "",
    agent_sec: "",
    bootstrap_sec: "",
    cpus: "",
    memory_mb: "",
  });
  // Attachment snapshot ids in load order.
  const [chosenAttachments, setChosenAttachments] = useState<string[]>([]);
  // An earlier model revision, pinned by an imported request until the model changes.
  const [modelRevision, setModelRevision] = useState<number | null>(null);
  const [planId, setPlanId] = useState("");
  const [planName, setPlanName] = useState("");
  const [collectionName, setCollectionName] = useState("");
  const [collectionScenario, setCollectionScenario] = useState<Scenario>("models");
  const [draft, setDraft] = useState<DraftMember[]>([]);
  const [batchName, setBatchName] = useState("");
  const [batchPreview, setBatchPreview] = useState<BatchPreview | null>(null);
  const batchKey = useRef<string>(newKey());
  const [parameters, setParameters] = useState<
    Record<string, { value: string; secret: boolean }>
  >({});
  const [clientRequestId, setClientRequestId] = useState("");
  const [resolution, setResolution] = useState<ConfigResolution | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  // One idempotency key per logical submission; kept while the outcome is unknown.
  const key = useRef<string>(newKey());
  const [pendingKey, setPendingKey] = useState<string | null>(null);

  const model =
    (models.data ?? []).find((m) => m.id === modelId) ?? models.data?.[0];
  const compatible = (harnesses.data ?? []).filter(
    (h) => h.enabled && (!model || h.protocols.includes(model.protocol)),
  );
  const harness = compatible.find((h) => h.id === harnessId) ?? compatible[0];
  const eligible = (tasks.data ?? []).filter((t) => t.can_submit);
  const attachmentById = new Map((attachments.data ?? []).map((a) => [a.id, a]));
  const addable = (attachments.data ?? []).filter(
    (a) =>
      !chosenAttachments.includes(a.id) &&
      (!harness ||
        a.compatible_harnesses.includes("*") ||
        a.compatible_harnesses.includes(harness.agent)),
  );
  const installs = chosenAttachments.some(
    (id) => attachmentById.get(id)?.has_bootstrap,
  );
  const moveAttachment = (index: number, step: number) => {
    const next = [...chosenAttachments];
    const [item] = next.splice(index, 1);
    next.splice(index + step, 0, item);
    changed(setChosenAttachments)(next);
  };
  // Author parameters declared by this version's tasks; required ones need a value.
  const declared = useMemo(() => {
    const found = new Map<
      string,
      { sensitive: boolean; required: boolean; default: string | null }
    >();
    for (const task of tasks.data ?? []) {
      for (const p of task.parameters) {
        const known = found.get(p.name);
        found.set(p.name, {
          sensitive: (known?.sensitive ?? false) || p.sensitive,
          required: (known?.required ?? false) || task.required_parameters.includes(p.name),
          default: known?.default ?? p.default,
        });
      }
    }
    return [...found.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [tasks.data]);

  // Any edit makes the shown resolution outdated: it has to be checked again.
  const changed =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      set(value);
      setResolution(null);
    };
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    changed(setSelected)(next);
  };

  const request = (): ConfigRequest => {
    const params: Record<string, ParameterValue> = {};
    for (const [pname, entry] of Object.entries(parameters)) {
      if (entry.value.trim() === "") continue;
      params[pname] = entry.secret
        ? { credential_id: entry.value.trim() }
        : entry.value;
    }
    const typedHarnessParams: Record<string, string | number | boolean> = {};
    for (const [pname, text] of Object.entries(harnessParams)) {
      if (text.trim() === "") continue;
      const spec = harness?.parameter_schema.parameters[pname];
      typedHarnessParams[pname] =
        spec?.type === "integer" || spec?.type === "number"
          ? Number(text)
          : spec?.type === "boolean"
            ? text === "true"
            : text;
    }
    return {
      dataset_version_id: versionId,
      execution_mode: executionMode,
      task_selection:
        mode === "all_eligible"
          ? { mode }
          : { mode, task_ids: [...selected].sort() },
      ...(executionMode === "agent"
        ? {
            model_config_id: model?.id ?? null,
            model_revision: modelRevision,
            harness_version_id: harness?.id ?? null,
            harness_params: typedHarnessParams,
            attachments: chosenAttachments,
          }
        : {}),
      repetitions: numberOrNull(overrides.repetitions),
      concurrency: numberOrNull(overrides.concurrency),
      run_deadline_hours: numberOrNull(overrides.run_deadline_hours),
      resources: {
        cpus: numberOrNull(overrides.cpus),
        memory_mb: numberOrNull(overrides.memory_mb),
      },
      timeouts: {
        agent_sec: numberOrNull(overrides.agent_sec),
        bootstrap_sec:
          executionMode === "agent" ? numberOrNull(overrides.bootstrap_sec) : null,
      },
      parameters: params,
    };
  };

  const plan = (plans.data ?? []).find((p) => p.id === planId);
  /** What is sent: with a plan, only the fields that differ from it. */
  const outgoing = (): Partial<ConfigRequest> => ({
    ...(plan
      ? { ...ownFields(request(), plan.template), plan_id: plan.id, plan_revision: plan.revision }
      : request()),
    ...(collectionId ? { collection_id: collectionId } : {}),
  });

  /** Fill the form from a (partial) request: a plan's template or a run's request. */
  const applyRequest = (req: PartialRequest) => {
    if (req.dataset_version_id) setChosenVersion(req.dataset_version_id);
    if (req.execution_mode) setExecutionMode(req.execution_mode);
    if (req.model_config_id) setModelId(req.model_config_id);
    setModelRevision(req.model_revision ?? null);
    if (req.harness_version_id) setHarnessId(req.harness_version_id);
    if (req.harness_params)
      setHarnessParams(
        Object.fromEntries(Object.entries(req.harness_params).map(([k, v]) => [k, String(v)])),
      );
    if (req.attachments) setChosenAttachments(req.attachments);
    if (req.task_selection) {
      setMode(req.task_selection.mode ?? "all_eligible");
      setSelected(new Set(req.task_selection.task_ids ?? []));
    }
    const text = (v: number | null | undefined) => (v == null ? "" : String(v));
    setOverrides({
      repetitions: text(req.repetitions),
      concurrency: text(req.concurrency),
      run_deadline_hours: text(req.run_deadline_hours),
      agent_sec: text(req.timeouts?.agent_sec),
      bootstrap_sec: text(req.timeouts?.bootstrap_sec),
      cpus: text(req.resources?.cpus),
      memory_mb: text(req.resources?.memory_mb),
    });
    if (req.parameters)
      setParameters(
        Object.fromEntries(
          Object.entries(req.parameters).map(([k, v]) =>
            typeof v === "string"
              ? [k, { value: v, secret: false }]
              : [k, { value: v.credential_id, secret: true }],
          ),
        ),
      );
    setResolution(null);
  };

  // "Use this plan" from the plans page.
  const wanted = params.get("plan");
  const planApplied = useRef(false);
  useEffect(() => {
    const chosen = (plans.data ?? []).find((p) => p.id === wanted);
    if (!chosen || planApplied.current) return;
    planApplied.current = true;
    setPlanId(chosen.id);
    applyRequest(chosen.template);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted, plans.data]);

  const collectionApplied = useRef(false);
  useEffect(() => {
    if (!collection.data || collectionApplied.current) return;
    collectionApplied.current = true;
    applyRequest(collection.data.template);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collection.data]);

  const imported = useRef(false);
  useEffect(() => {
    if (!sourceRunId || imported.current) return;
    imported.current = true;
    void (async () => {
      try {
        const [req, run] = await Promise.all([
          api.get("/runs/{run_id}/request", { path: { run_id: sourceRunId } }),
          api.get("/runs/{run_id}", { path: { run_id: sourceRunId } }),
        ]);
        applyRequest(req.data);
        setName(`${run.data.name}（修改）`);
      } catch (err) {
        setError(err);
      }
    })();
    // applyRequest only sets state; it is safe to leave out of the dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceRunId]);

  const savePlan = async () => {
    setError(undefined);
    try {
      const res = await api.post("/plans", {
        body: {
          name: planName.trim(),
          template: setFields(request()),
        },
      });
      toast.success(`已保存方案“${res.data.name}”`);
      setPlanName("");
      await plans.reload();
    } catch (err) {
      setError(err);
    }
  };

  const saveCollection = async () => {
    setError(undefined);
    try {
      const res = await api.post("/collections", {
        body: {
          name: collectionName.trim(),
          scenario: collectionScenario,
          base: { request: setFields(request()) },
        },
      });
      toast.success(`已建好集合“${res.data.name}”`);
      navigate(`/collections/${res.data.id}`);
    } catch (err) {
      setError(err);
    }
  };

  const addToDraft = () => {
    const label = [
      executionMode === "agent" ? (model?.name ?? "模型") : "Oracle",
      harness ? `${harness.name} ${harness.version}` : null,
      chosenAttachments.length ? `${chosenAttachments.length} 个附件` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    setDraft([...draft, { name: name.trim() || `成员 ${draft.length + 1}`, request: outgoing(), label }]);
    setBatchPreview(null);
    toast.success("已加入批次草稿");
  };
  const batchBody = () => ({
    name: batchName.trim(),
    runs: draft.map((m) => ({ name: m.name, ...m.request })),
  });
  const previewBatch = async () => {
    setError(undefined);
    try {
      setBatchPreview((await api.post("/run-batches/preview", { body: batchBody() })).data);
    } catch (err) {
      setError(err);
    }
  };
  const submitBatch = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/run-batches", {
        body: batchBody(),
        headers: {
          "Idempotency-Key": batchKey.current,
        },
      });
      batchKey.current = newKey();
      toast.success(`批次已受理：${res.data.runs.length} 个运行，${res.data.planned_trials} 个试次`);
      navigate(`/batches/${res.data.batch_id}`);
    } catch (err) {
      setError(err);
      // An unknown outcome keeps the key, so a retry cannot accept the batch twice.
      if (!(err instanceof ApiError && err.indeterminate)) batchKey.current = newKey();
    } finally {
      setBusy(false);
    }
  };

  const check = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/config-resolutions", { body: outgoing() });
      setResolution(res.data);
    } catch (err) {
      setError(err);
      setResolution(null);
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!resolution) return;
    setBusy(true);
    setError(undefined);
    const body: RunCreate = {
      name,
      resolution_id: resolution.resolution_id,
      client_request_id: clientRequestId || null,
      source_run_id: sourceRunId,
      collection_id: collectionId,
    };
    try {
      const res = await api.post("/runs", {
        body: body,
        headers: {
          "Idempotency-Key": key.current,
        },
      });
      key.current = newKey();
      setPendingKey(null);
      toast.success(`已受理：计划 ${res.data.planned_trials} 个试次`);
      navigate(`/runs/${res.data.run_id}`);
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && err.indeterminate) {
        setPendingKey(key.current);
      } else {
        key.current = newKey();
        setPendingKey(null);
        if (
          err instanceof ApiError &&
          err.code.startsWith("CONFIG_RESOLUTION_")
        )
          setResolution(null);
      }
    } finally {
      setBusy(false);
    }
  };

  const fieldErr = (field: string) =>
    error instanceof ApiError ? error.field(field) : undefined;
  const override = (
    field: keyof typeof overrides,
    label: string,
    hint: string,
  ) => (
    <TextField
      id={`run-${field}`}
      label={label}
      type="number"
      min={1}
      step={1}
      placeholder="继承"
      value={overrides[field]}
      disabled={locked(OVERRIDE_DIMENSION[field] ?? "")}
      onChange={(e) =>
        changed(setOverrides)({ ...overrides, [field]: e.target.value })
      }
      hint={hint}
      error={fieldErr(field)}
    />
  );

  return (
    <div>
      <PageTitle title="新建运行">
        <Button asChild variant="outline" size="sm">
          <Link to="/runs">返回列表</Link>
        </Button>
      </PageTitle>
      {datasets.loading && !datasets.data && <Loading />}
      <ErrorAlert error={datasets.error} title="无法加载数据集" />
      {datasets.data && versions.length === 0 && (
        <EmptyNote>还没有可用的数据集版本，请管理员先在“数据集”页添加。</EmptyNote>
      )}
      {versions.length > 0 && (
        <form className="grid gap-4" onSubmit={check}>
          {collectionId && (
            <div className="rounded-md border border-sky-300 bg-sky-50 p-3 text-sm text-sky-900">
              从集合{" "}
              <Link to={`/collections/${collectionId}`} className="underline">
                {collection.data?.name ?? collectionId}
              </Link>{" "}
              新建：固定的维度（
              {[...fixedDimensions].map((d) => DIMENSION[d as Dimension] ?? d).join("、")}
              ）已按集合填好并锁住，只需选可变的维度、任务和并发。提交后自动加入集合；与集合不符的配置在“检查配置”时就会指出。
            </div>
          )}
          {sourceRunId && (
            <div className="rounded-md border border-sky-300 bg-sky-50 p-3 text-sm text-sky-900">
              基于运行{" "}
              <Link to={`/runs/${sourceRunId}`} className="underline">
                {sourceRunId}
              </Link>{" "}
              修改：表单按原运行实际用的值填好（团队默认与模型修订已固定）。提交后会记录与原运行的差异。
            </div>
          )}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">实验方案</CardTitle>
              <CardDescription>
                方案是保存下来的一组配置，团队可见。选用后表单按方案填好；提交时仍引用这个方案，你改过的字段记为“本次填写”，其余记为“实验方案”。
                <Link to="/plans" className="ml-1 underline">
                  管理方案
                </Link>
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-2">
              <SelectField
                id="run-plan"
                label="选用方案"
                value={planId}
                onChange={(e) => {
                  const chosen = (plans.data ?? []).find((p) => p.id === e.target.value);
                  setPlanId(e.target.value);
                  if (chosen) applyRequest(chosen.template);
                  else setResolution(null);
                }}
              >
                <option value="">不使用方案</option>
                {(plans.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}（{p.owner_username}，修订 {p.revision}）
                  </option>
                ))}
              </SelectField>
              <div className="flex items-end gap-2 md:col-span-2">
                <TextField
                  id="collection-from-form"
                  label="把当前表单建成集合"
                  placeholder="集合名称"
                  value={collectionName}
                  onChange={(e) => setCollectionName(e.target.value)}
                  hint="固定维度取表单当前的值；之后可以从集合新建运行"
                />
                <select
                  aria-label="集合场景"
                  className="mb-[1.35rem] h-9 rounded-md border bg-background px-2 text-sm"
                  value={collectionScenario}
                  onChange={(e) => setCollectionScenario(e.target.value as Scenario)}
                >
                  {Object.entries(SCENARIO)
                    .filter(([, s]) => s.variable)
                    .map(([key, s]) => (
                      <option key={key} value={key}>
                        {s.label}
                      </option>
                    ))}
                </select>
                <Button
                  type="button"
                  variant="outline"
                  className="mb-[1.35rem]"
                  disabled={!collectionName.trim() || executionMode !== "agent"}
                  onClick={() => void saveCollection()}
                >
                  建成集合
                </Button>
              </div>
              <div className="flex items-end gap-2">
                <TextField
                  id="plan-name"
                  label="把当前表单另存为方案"
                  placeholder="方案名称"
                  value={planName}
                  onChange={(e) => setPlanName(e.target.value)}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={!planName.trim()}
                  onClick={() => void savePlan()}
                >
                  保存
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">基本信息</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-2">
              <TextField
                id="run-name"
                label="运行名称"
                value={name}
                onChange={(e) => setName(e.target.value)}
                error={fieldErr("name")}
                required
              />
              <SelectField
                id="run-version"
                label="数据集版本"
                value={versionId}
                disabled={!!collectionId}
                onChange={(e) => {
                  changed(setChosenVersion)(e.target.value);
                  setSelected(new Set());
                  setParameters({});
                }}
                error={fieldErr("dataset_version_id")}
              >
                {versions.map(({ dataset, version }) => (
                  <option key={version.id} value={version.id}>
                    {dataset.namespace}/{dataset.name} @ {version.upstream_ref} ({version.status})
                  </option>
                ))}
              </SelectField>
              <fieldset className="grid gap-1 text-sm md:col-span-2">
                <legend className="mb-1">执行方式</legend>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="execution-mode"
                    disabled={!!collectionId}
                    checked={executionMode === "agent"}
                    onChange={() => changed(setExecutionMode)("agent")}
                  />
                  模型实验：由 Harness 驱动所选模型完成任务
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="execution-mode"
                    disabled={!!collectionId}
                    checked={executionMode === "oracle"}
                    onChange={() => changed(setExecutionMode)("oracle")}
                  />
                  Oracle 测试模式：执行任务自带的参考解法，不调用模型，不参与模型比较
                </label>
              </fieldset>
              <TextField
                id="run-client-request"
                label="客户端请求标识（可选）"
                value={clientRequestId}
                onChange={(e) => setClientRequestId(e.target.value)}
                error={fieldErr("client_request_id")}
                hint="便于之后按此标识查找该运行"
              />
            </CardContent>
          </Card>

          {executionMode === "agent" && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">模型与 Harness</CardTitle>
                <CardDescription>模型配置与 Harness 由管理员维护；这里只能选择。</CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                {models.data && models.data.length === 0 && (
                  <EmptyNote>还没有启用的模型配置，请管理员在“管理 → 模型”中添加。</EmptyNote>
                )}
                {models.data && models.data.length > 0 && (
                  <SelectField
                    id="run-model"
                    label="模型"
                    value={model?.id ?? ""}
                    disabled={locked("model")}
                    onChange={(e) => {
                      changed(setModelId)(e.target.value);
                      setModelRevision(null);
                    }}
                    error={fieldErr("model_config_id")}
                  >
                    {models.data.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name} — {m.model_id}（{m.provider}，修订 {m.revision}）
                        {m.credential_problem ? " ⚠ 凭据不可用" : ""}
                      </option>
                    ))}
                  </SelectField>
                )}
                {modelRevision != null && model && modelRevision !== model.revision && (
                  <p className="text-xs text-amber-800 md:col-span-2">
                    固定在原运行用的修订 {modelRevision}（当前是修订 {model.revision}）。
                    <button
                      type="button"
                      className="ml-1 underline"
                      onClick={() => changed(setModelRevision)(null)}
                    >
                      改用当前修订
                    </button>
                  </p>
                )}
                {model && compatible.length === 0 && (
                  <EmptyNote>没有能调用这个模型协议的 Harness 版本。</EmptyNote>
                )}
                {compatible.length > 0 && (
                  <SelectField
                    id="run-harness"
                    label="Harness"
                    value={harness?.id ?? ""}
                    disabled={locked("harness")}
                    onChange={(e) => {
                      changed(setHarnessId)(e.target.value);
                      setHarnessParams({});
                    }}
                    error={fieldErr("harness_version_id")}
                  >
                    {compatible.map((h) => (
                      <option key={h.id} value={h.id}>
                        {h.name} {h.version}（{h.agent}）
                      </option>
                    ))}
                  </SelectField>
                )}
                {harness &&
                  Object.entries(harness.parameter_schema.parameters).map(([pname, spec]) => (
                    <TextField
                      key={pname}
                      id={`harness-param-${pname}`}
                      label={`Harness 参数 ${pname}（${spec.type}）`}
                      placeholder={
                        spec.default !== undefined ? `默认 ${String(spec.default)}` : "未设置"
                      }
                      value={harnessParams[pname] ?? ""}
                      onChange={(e) =>
                        changed(setHarnessParams)({
                          ...harnessParams,
                          [pname]: e.target.value,
                        })
                      }
                      hint={spec.description ?? undefined}
                    />
                  ))}
                <div className="grid gap-2 md:col-span-2">
                  <p className="text-sm font-medium">附件（按加载顺序）</p>
                  <p className="text-xs text-muted-foreground">
                    每个试次在自己的容器里依次复制并安装这些附件，然后才开始 Agent。附件由成员在
                    <Link to="/attachments" className="mx-1 underline">
                      附件页
                    </Link>
                    发布。
                  </p>
                  {chosenAttachments.length > 0 && (
                    <ol className="grid gap-1 text-sm">
                      {chosenAttachments.map((id, index) => {
                        const a = attachmentById.get(id);
                        return (
                          <li key={id} className="flex flex-wrap items-center gap-2">
                            <span className="w-5 text-right text-xs text-muted-foreground">
                              {index + 1}.
                            </span>
                            <span className="font-medium">{a?.name ?? id}</span>
                            <span className="font-mono text-xs">{a && snapshotName(a)}</span>
                            {a?.has_bootstrap && <Pill tone="info">安装脚本</Pill>}
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={index === 0}
                              onClick={() => moveAttachment(index, -1)}
                            >
                              上移
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={index === chosenAttachments.length - 1}
                              onClick={() => moveAttachment(index, 1)}
                            >
                              下移
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                changed(setChosenAttachments)(
                                  chosenAttachments.filter((x) => x !== id),
                                )
                              }
                            >
                              移除
                            </Button>
                          </li>
                        );
                      })}
                    </ol>
                  )}
                  {addable.length > 0 && (
                    <SelectField
                      id="run-attachment-add"
                      label="添加附件"
                      value=""
                      onChange={(e) =>
                        e.target.value &&
                        changed(setChosenAttachments)([...chosenAttachments, e.target.value])
                      }
                      error={fieldErr("attachments")}
                    >
                      <option value="">选择要加载的附件</option>
                      {addable.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name} {snapshotName(a)}（{a.owner_username ?? "—"}）
                        </option>
                      ))}
                    </SelectField>
                  )}
                  {installs &&
                    override(
                      "bootstrap_sec",
                      "附件安装时限（秒，每试次）",
                      `团队默认 ${limit(capabilities, "bootstrap_timeout_sec", 600)}，上限 ${limit(capabilities, "bootstrap_timeout_sec_max", 3600)}`,
                    )}
                </div>
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">规模与资源</CardTitle>
              <CardDescription>
                留空表示继承：次数、并发和总时限取团队默认，资源与时限取任务作者的设定。超过团队上限会被拒绝，不会被悄悄改小。
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-3">
              {override(
                "repetitions",
                "每任务重复次数",
                `团队默认 ${limit(capabilities, "repetitions_default", 1)}，上限 ${limit(capabilities, "repetitions_max", 20)}`,
              )}
              {override(
                "concurrency",
                "并发数",
                `团队默认 ${limit(capabilities, "run_default_concurrency", 4)}，上限 ${limit(capabilities, "run_max_concurrency", 10)}`,
              )}
              {override(
                "run_deadline_hours",
                "运行总时限（小时，含排队）",
                `团队默认 ${limit(capabilities, "run_deadline_hours", 72)}，上限 ${limit(capabilities, "run_deadline_hours_max", 72)}`,
              )}
              {override("agent_sec", "Agent 时限（秒）", "留空用各任务作者的设定")}
              {override(
                "cpus",
                "每试次 CPU",
                "试次内所有容器之和；留空用作者设定，只能调高，多出的给主环境",
              )}
              {override(
                "memory_mb",
                "每试次内存（MiB）",
                "试次内所有容器之和；留空用作者设定，只能调高，多出的给主环境",
              )}
            </CardContent>
          </Card>

          {declared.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">作者参数</CardTitle>
                <CardDescription>
                  任务作者声明的参数。秘密参数只能选一个团队凭据，不能直接填值；凭据由管理员维护，这里只显示名称。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                {declared.map(([pname, info]) =>
                  info.sensitive ? (
                    <SelectField
                      key={pname}
                      id={`param-${pname}`}
                      label={`${pname}${info.required ? "（必填）" : ""}（秘密）`}
                      value={parameters[pname]?.value ?? ""}
                      onChange={(e) =>
                        changed(setParameters)({
                          ...parameters,
                          [pname]: { value: e.target.value, secret: true },
                        })
                      }
                      error={fieldErr(pname)}
                    >
                      <option value="">{info.default != null ? "用作者默认值" : "选择凭据"}</option>
                      {(credentials.data ?? [])
                        .filter((c) => c.kind === "generic_secret")
                        .map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.name}（{c.provider}）
                          </option>
                        ))}
                    </SelectField>
                  ) : (
                    <TextField
                      key={pname}
                      id={`param-${pname}`}
                      label={`${pname}${info.required ? "（必填）" : ""}`}
                      placeholder={info.default != null ? `默认 ${info.default}` : ""}
                      value={parameters[pname]?.value ?? ""}
                      onChange={(e) =>
                        changed(setParameters)({
                          ...parameters,
                          [pname]: { value: e.target.value, secret: false },
                        })
                      }
                      error={fieldErr(pname)}
                    />
                  ),
                )}
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">任务选择</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3">
              {detail.data && (
                <p className="text-sm text-muted-foreground">
                  共 {detail.data.task_count} 个任务；已准入 {detail.data.admitted_task_count}；
                  可提交 {detail.data.submittable_task_count}。
                </p>
              )}
              <div className="flex flex-wrap gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="mode"
                    checked={mode === "all_eligible"}
                    onChange={() => changed(setMode)("all_eligible")}
                  />
                  全部可提交任务（{eligible.length}）
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="mode"
                    checked={mode === "explicit"}
                    onChange={() => changed(setMode)("explicit")}
                  />
                  手动选择（已选 {selected.size}）
                </label>
              </div>
              <ErrorAlert error={tasks.error} title="无法加载任务" />
              {tasks.loading && !tasks.data && <Loading />}
              {tasks.data && (
                <div className="max-h-96 overflow-auto rounded-md border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        {mode === "explicit" && <TableHead className="w-10" />}
                        <TableHead>任务</TableHead>
                        <TableHead>准入</TableHead>
                        <TableHead>内容</TableHead>
                        <TableHead>本机缓存</TableHead>
                        <TableHead>可提交</TableHead>
                        <TableHead>说明</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {tasks.data.map((task) => (
                        <TableRow key={task.id}>
                          {mode === "explicit" && (
                            <TableCell>
                              <input
                                type="checkbox"
                                aria-label={`选择 ${task.task_key}`}
                                disabled={!task.can_submit}
                                checked={selected.has(task.id)}
                                onChange={() => toggle(task.id)}
                              />
                            </TableCell>
                          )}
                          <TableCell className="max-w-[24rem] font-mono text-xs">
                            <LongName value={task.task_key} prefix={taskPrefix} />
                            {task.required_parameters.length > 0 && (
                              <div className="text-[11px] text-muted-foreground">
                                需要参数：{task.required_parameters.join("、")}
                              </div>
                            )}
                          </TableCell>
                          <TaskStatusCells task={task} />
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>

          <ErrorAlert error={error} title={resolution ? "提交失败" : "检查失败"} />
          <div>
            <Button
              type="submit"
              variant={resolution ? "outline" : "default"}
              disabled={busy || !versionId || (mode === "explicit" && selected.size === 0)}
            >
              {busy && !resolution ? "检查中…" : resolution ? "重新检查" : "检查配置"}
            </Button>{" "}
            <Button
              type="button"
              variant="outline"
              disabled={busy || !versionId || sourceRunId != null}
              title={sourceRunId ? "修改后运行单独提交" : undefined}
              onClick={addToDraft}
            >
              加入批次草稿
            </Button>
          </div>

          {resolution && (
            <ResolutionReview
              resolution={resolution}
              busy={busy}
              pendingKey={pendingKey}
              canSubmit={name.trim() !== ""}
              onSubmit={() => void submit()}
            />
          )}

          {draft.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">批次草稿（{draft.length} 个运行）</CardTitle>
                <CardDescription>
                  批次里的运行一起受理：任何一个不能提交，整批都不受理，也不扣令牌额度。受理后各自执行。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-3 text-sm">
                <ol className="grid gap-1">
                  {draft.map((m, i) => {
                    const found = batchPreview?.runs[i];
                    return (
                      <li key={i} className="flex flex-wrap items-center gap-2">
                        <span className="w-5 text-right text-xs text-muted-foreground">
                          {i + 1}.
                        </span>
                        <span className="font-medium">{m.name}</span>
                        <span className="text-xs text-muted-foreground">{m.label}</span>
                        {found && (
                          <span className="text-xs">
                            {found.blockers && found.blockers.length > 0 ? (
                              <span className="text-destructive">
                                {found.blockers
                                  .map((b) => CONFIG_BLOCKER[b.code] ?? b.message)
                                  .join("；")}
                              </span>
                            ) : (
                              `${found.planned_trials} 个试次`
                            )}
                          </span>
                        )}
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setDraft(draft.filter((_, j) => j !== i));
                            setBatchPreview(null);
                          }}
                        >
                          移除
                        </Button>
                      </li>
                    );
                  })}
                </ol>
                {batchPreview && (
                  <p className={batchPreview.can_submit ? "text-emerald-800" : "text-destructive"}>
                    共 {batchPreview.planned_trials} 个试次
                    {batchPreview.pat_quota &&
                      `，需要令牌额度 ${batchPreview.pat_quota.needed}（剩 ${batchPreview.pat_quota.left}）`}
                    {batchPreview.duplicate_configs.length > 0 && "；有重复的配置"}
                    {batchPreview.can_submit ? "，可以提交" : "，不能提交"}
                  </p>
                )}
                <div className="flex flex-wrap items-end gap-2">
                  <TextField
                    id="batch-name"
                    label="批次名称"
                    value={batchName}
                    onChange={(e) => setBatchName(e.target.value)}
                  />
                  <Button type="button" variant="outline" onClick={() => void previewBatch()}>
                    预览
                  </Button>
                  <Button
                    type="button"
                    disabled={busy || !batchName.trim() || !batchPreview?.can_submit}
                    onClick={() => void submitBatch()}
                  >
                    提交批次
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </form>
      )}
    </div>
  );
}

const FIELD_LABEL: Record<string, string> = {
  "/repetitions": "每任务重复次数",
  "/concurrency": "并发数",
  "/run_deadline_hours": "运行总时限（小时）",
  "/resources/cpus": "每试次 CPU",
  "/resources/memory_mb": "每试次内存（MiB）",
  "/timeouts/agent_sec": "Agent 时限（秒）",
  "/timeouts/verifier_sec": "验证时限（秒）",
  "/model": "模型",
  "/harness": "Harness",
  "/attachments": "附件",
  "/timeouts/bootstrap_sec": "附件安装时限（秒）",
};

const TASK_FIELD: Record<string, keyof EffectiveTask> = {
  "/resources/cpus": "cpus",
  "/resources/memory_mb": "memory_mb",
  "/timeouts/agent_sec": "agent_timeout_sec",
  "/timeouts/verifier_sec": "verifier_timeout_sec",
};

function ResolutionReview({
  resolution,
  busy,
  pendingKey,
  canSubmit,
  onSubmit,
}: {
  resolution: ConfigResolution;
  busy: boolean;
  pendingKey: string | null;
  canSubmit: boolean;
  onSubmit: () => void;
}) {
  const effective = resolution.effective_config;
  const value = (path: string): string => {
    if (!effective) return "—";
    if (path === "/model") return resolution.labels.model ?? "—";
    if (path === "/harness") return resolution.labels.harness ?? "—";
    if (path === "/attachments") return resolution.labels.attachments || "—";
    if (path === "/timeouts/bootstrap_sec")
      return String(effective.bootstrap_timeout_sec ?? "—");
    const taskField = TASK_FIELD[path];
    if (taskField) {
      // Per-task values: one value when all tasks agree, otherwise the distinct ones.
      const values = new Set(
        effective.tasks.map((task) => {
          const v = task[taskField];
          return v == null ? "执行器默认" : String(v);
        }),
      );
      return [...values].join(" / ") || "—";
    }
    let node: unknown = effective;
    for (const part of path.split("/").filter(Boolean)) {
      node = (node as Record<string, unknown> | undefined)?.[part];
    }
    if (node && typeof node === "object")
      return "value" in node ? String(node.value) : "（凭据）";
    return node == null ? "—" : String(node);
  };
  const blocked = resolution.blockers.length > 0;

  return (
    <Card className={blocked ? "border-destructive" : "border-emerald-400"}>
      <CardHeader>
        <CardTitle className="text-base">
          {blocked
            ? "配置有阻塞项，不能提交"
            : `检查通过：计划 ${resolution.planned_trials} 个试次`}
        </CardTitle>
        <CardDescription>
          结果在 {fmtTime(resolution.expires_at)}{" "}
          前有效，不预留容量或额度；提交时服务端会再核对一次，期间配置若有变化会要求重新检查。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        {blocked && (
          <ul className="grid gap-1">
            {resolution.blockers.map((b, i) => (
              <li key={`${b.path}-${b.code}-${i}`} className="text-destructive">
                <code className="text-xs">{b.path}</code>：
                {CONFIG_BLOCKER[b.code] ?? b.message}
                <span className="text-xs text-muted-foreground">
                  （{b.message}）
                </span>
              </li>
            ))}
          </ul>
        )}
        {resolution.warnings.map((w, i) => (
          <div
            key={i}
            className="rounded-md border border-amber-300 bg-amber-50 p-2 text-amber-900"
          >
            <code className="text-xs">{w.path}</code>：{w.message}
          </div>
        ))}
        {effective && (
          <div className="overflow-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>字段</TableHead>
                  <TableHead>生效值</TableHead>
                  <TableHead>来源</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(resolution.field_sources).map(
                  ([path, source]) => (
                    <TableRow key={path}>
                      <TableCell>
                        {FIELD_LABEL[path] ?? (
                          <code className="text-xs">{path}</code>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {value(path)}
                      </TableCell>
                      <TableCell>
                        <Pill
                          tone={source.layer === "override" ? "warn" : "muted"}
                        >
                          {SOURCE_LAYER[source.layer] ?? source.layer}
                          {source.revision != null
                            ? ` · 修订 ${source.revision}`
                            : ""}
                        </Pill>
                      </TableCell>
                    </TableRow>
                  ),
                )}
              </TableBody>
            </Table>
          </div>
        )}
        {resolution.baseline_differences.length > 0 && (
          <p className="text-muted-foreground">
            与作者基准不同：
            {resolution.baseline_differences
              .map(
                (d) =>
                  `${FIELD_LABEL[d.path] ?? d.path} = ${d.value}（低于作者 ${d.tasks_lower} 个任务，高于 ${d.tasks_higher} 个）`,
              )
              .join("；")}
          </p>
        )}
        {resolution.excluded_tasks.length > 0 && (
          <div>
            <p className="font-medium">
              不会运行的任务（{resolution.excluded_tasks.length}）
            </p>
            <ul className="mt-1 grid gap-1 text-xs">
              {resolution.excluded_tasks.map((x) => (
                <li key={x.task_id}>
                  <code>{x.task_key}</code>：
                  {x.blockers.map(blockerText).join("；")}
                </li>
              ))}
            </ul>
          </div>
        )}
        {resolution.required_preparation.length > 0 && (
          <p className="text-muted-foreground">
            {resolution.required_preparation.length}{" "}
            个任务的本机缓存不在，运行开始前会自动恢复，无需管理员操作。
          </p>
        )}
        <JsonBlock
          value={resolution.effective_config}
          summary="完整生效配置（JSON）"
        />
        <p className="font-mono text-xs text-muted-foreground">
          配置摘要 {resolution.config_digest}
        </p>
        {pendingKey && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900">
            上次提交结果未知（网络或服务端错误）。再次提交会复用同一幂等键，不会重复创建运行。
            <Pill tone="muted">Idempotency-Key {pendingKey.slice(0, 8)}…</Pill>
          </div>
        )}
        <div>
          <Button
            type="button"
            disabled={busy || blocked || !canSubmit}
            onClick={onSubmit}
          >
            {busy ? "提交中…" : pendingKey ? "重试提交" : "按此配置提交运行"}
          </Button>
          {!canSubmit && (
            <span className="ml-2 text-xs text-muted-foreground">
              先填写运行名称
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
