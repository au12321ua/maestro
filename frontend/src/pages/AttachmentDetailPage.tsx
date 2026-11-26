import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { AttachmentPagination } from "@/components/AttachmentPagination";
import { AttachmentPublish } from "@/components/AttachmentPublish";
import { AttachmentComparison, AttachmentSnapshotFiles } from "@/components/AttachmentSnapshotFiles";
import { EmptyNote, ErrorAlert, Loading, PageTitle, Pill } from "@/components/common";
import { DIRECTORY_PAGE_SIZE, DirectoryCheck, TopicDirectory, TopicMembershipActions } from "@/components/TopicDirectory";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { WorkflowRecords } from "@/components/WorkflowRecords";
import { t } from "@/i18n";
import { API, api } from "@/lib/api";
import { attachmentKind, attachmentPath, directoryParams, type AttachmentKind } from "@/lib/attachment-directory";
import { bulkRecycle } from "@/lib/bulk-recycle";
import { fmtBytes, fmtTime, snapshotName } from "@/lib/format";
import { useLoad, useSession } from "@/lib/hooks";
import { useRecords } from "@/lib/records";
import { useTopicScope } from "@/lib/topics";
import type { Attachment, AttachmentVersion } from "@/lib/types";

type SnapshotLink = (id?: string, base?: string) => { to: string; state: unknown };

function ParentLink({ version, toSnapshot }: {
  version: AttachmentVersion;
  toSnapshot: SnapshotLink;
}) {
  if (!version.parent_id) return <span className="text-muted-foreground">{t("attachments.publish.noParent")}</span>;
  if (!version.parent) return <span className="text-muted-foreground" title={version.parent_id}>{t("attachments.list.deletedSnapshot")}</span>;
  return (
    <Link className="text-primary hover:underline" {...toSnapshot(version.parent.id)}>
      {snapshotName(version.parent)}
    </Link>
  );
}

function VersionBadges({ version }: { version: AttachmentVersion }) {
  return (
    <div className="flex flex-wrap gap-1">
      {version.deletion && <Pill tone="muted">{t("topics.objectRecycled")}</Pill>}
      {!version.enabled && <Pill tone="muted">{t("attachments.list.disabled")}</Pill>}
      {version.has_bootstrap && <Pill tone="neutral">{t("attachments.list.hasBootstrap")}</Pill>}
      {version.child_count > 1 && <Pill tone="warn">{t("attachments.history.branches", { count: version.child_count })}</Pill>}
    </div>
  );
}

function SelectedSnapshot({ id, name, kind, base, revision, path, mayManage, recycling, onPublish, onRecycle, onChanged }: {
  id: string;
  name: string;
  kind: AttachmentKind;
  base: string | null;
  revision: number;
  path: SnapshotLink;
  mayManage: (item: Attachment) => boolean;
  recycling: boolean;
  onPublish: (item: Attachment) => void;
  onRecycle: (items: Attachment[]) => void;
  onChanged: () => void;
}) {
  const { topicId } = useTopicScope();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const selected = useLoad(async () => {
    const [version, files] = await Promise.all([
      api.get("/attachment-directory/version", { query: { snapshot_id: id, topic_id: topicId } }),
      api.get("/attachments/{attachment_id}", { path: { attachment_id: id } }),
    ]);
    return { ...version.data, detail: files.data };
  }, [revision]);

  useEffect(() => {
    document.getElementById("attachment-selected")?.scrollIntoView({ block: "nearest" });
  }, [id, base]);

  const toggleEnabled = async (item: Attachment) => {
    setError(undefined);
    setBusy(true);
    try {
      await api.post("/attachments/{attachment_id}/enabled", {
        path: { attachment_id: item.id },
        body: { enabled: !item.enabled },
      });
      toast.success(t(item.enabled ? "attachments.list.disabledDone" : "attachments.list.enabledDone"));
      onChanged();
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  };

  if (!selected.data || selected.data.detail.name !== name || selected.data.detail.kind !== kind) {
    return (
      <Card id="attachment-selected" className="scroll-mt-4">
        <CardContent className="grid gap-2">
          {selected.loading && <Loading />}
          <ErrorAlert error={selected.error} />
          {selected.data && <p role="status">{t("attachments.detail.invalidSnapshot")}</p>}
          <div>
            <Button asChild size="sm" variant="ghost">
              <Link {...path()}>{t("attachments.detail.close")}</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  const { snapshot, detail, in_topic } = selected.data;
  const baseId = base ?? snapshot.parent?.id;
  return (
    <Card id="attachment-selected" className="scroll-mt-4">
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="grid gap-1">
            <CardTitle className="text-base">{snapshotName(snapshot)}</CardTitle>
            <code className="break-all text-xs text-muted-foreground">{snapshot.id}</code>
          </div>
          <Button variant="ghost" size="sm" asChild>
            <Link {...path()}>{t("attachments.detail.close")}</Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <ErrorAlert error={error ?? selected.error} />
        {topicId && !in_topic && <p role="status" className="text-muted-foreground">{t("attachments.detail.outsideTopic")}</p>}
        <VersionBadges version={snapshot} />
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{fmtTime(snapshot.created_at)} · {snapshot.owner_username ?? snapshot.owner_id}</span>
          <span>{t("attachments.list.files", { count: snapshot.file_count, size: fmtBytes(snapshot.size_bytes) })}</span>
        </div>
        <div>
          {t("attachments.publish.parent")} · <ParentLink version={snapshot} toSnapshot={path} />
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" asChild>
            <a href={`${API}/attachments/${encodeURIComponent(id)}/archive`}>{t("attachments.download")}</a>
          </Button>
          {!detail.deletion && (
            <Button size="sm" variant="outline" onClick={() => onPublish(detail)}>{t("attachments.list.baseOn")}</Button>
          )}
          {mayManage(detail) && (
            <>
              <Button size="sm" variant="outline" disabled={busy || recycling} onClick={() => void toggleEnabled(detail)}>
                {t(detail.enabled ? "attachments.list.disable" : "attachments.list.enable")}
              </Button>
              <Button size="sm" variant="outline" disabled={busy || recycling} onClick={() => onRecycle([detail])}>
                {t("attachments.recycle.action")}
              </Button>
            </>
          )}
        </div>
        <AttachmentSnapshotFiles detail={detail} />
        {baseId && (
          <div className="grid gap-2">
            <AttachmentComparison key={baseId} detail={detail} baseId={baseId} />
            <div>
              <Button asChild size="sm" variant="ghost">
                <Link {...path(id, "")}>{t("attachments.detail.noCompare")}</Link>
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RecycleConfirmation({ name, items, busy, onCancel, onConfirm }: {
  name: string;
  items: Attachment[];
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const cancelButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelButton.current?.focus();
  }, []);
  return (
    <Alert>
      <AlertTitle>{t("attachments.recycle.confirm", { name, count: items.length })}</AlertTitle>
      <AlertDescription className="grid gap-3">
        <ul className="max-h-48 overflow-auto">
          {items.map((item) => <li key={item.id} className="break-all">{snapshotName(item)} · <code>{item.id}</code></li>)}
        </ul>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy} variant="destructive" onClick={onConfirm}>{t("attachments.recycle.action")}</Button>
          <Button ref={cancelButton} size="sm" disabled={busy} variant="outline" onClick={onCancel}>{t("topics.cancel")}</Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

function VersionResults({ name, kind, topicId, q, pages, revision, selected, focused, path, onToggle, onPage }: {
  name: string;
  kind: AttachmentKind;
  topicId: string | null;
  q: string;
  pages: string[];
  revision: number;
  selected: ReadonlyMap<string, Attachment>;
  focused: string | null;
  path: SnapshotLink;
  onToggle: (item: Attachment) => void;
  onPage: (cursor?: string) => void;
}) {
  const list = useLoad(() => api.get("/attachment-directory/versions", {
    query: { name, kind, topic_id: topicId, q, cursor: pages.at(-1), limit: DIRECTORY_PAGE_SIZE },
  }), [revision]);

  return (
    <>
      {list.loading && !list.data && <Loading />}
      <ErrorAlert error={list.error} />
      {list.data?.data.length === 0 && <EmptyNote>{t("attachments.detail.empty")}</EmptyNote>}
      {!!list.data?.data.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead />
              <TableHead>{t("attachments.detail.snapshot")}</TableHead>
              <TableHead>{t("attachments.directory.published")}</TableHead>
              <TableHead>{t("attachments.publish.parent")}</TableHead>
              {focused && <TableHead />}
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.data.data.map((item) => (
              <TableRow key={item.id} data-state={selected.has(item.id) ? "selected" : undefined}>
                <TableCell>
                  <DirectoryCheck name={`${name} · ${snapshotName(item)}`} checked={selected.has(item.id)} onChange={() => onToggle(item)} />
                </TableCell>
                <TableCell>
                  <Link {...path(item.id)} aria-current={item.id === focused ? "true" : undefined}
                    className="font-medium text-primary hover:underline">
                    {snapshotName(item)}
                  </Link>
                  <VersionBadges version={item} />
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs">{fmtTime(item.created_at)}</TableCell>
                <TableCell><ParentLink version={item} toSnapshot={path} /></TableCell>
                {focused && (
                  <TableCell>
                    {item.id !== focused && (
                      <Button asChild size="sm" variant="ghost">
                        <Link {...path(focused, item.id)} aria-label={t("attachments.detail.compareAgainst", { snapshot: snapshotName(item) })}>
                          {t("attachments.detail.compare")}
                        </Link>
                      </Button>
                    )}
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <AttachmentPagination page={pages.length + 1} next={list.data?.meta.next_cursor} loading={list.loading}
        onPrevious={() => onPage()} onNext={onPage} />
    </>
  );
}

function AttachmentDetail({ name, kind }: { name: string; kind: AttachmentKind }) {
  const [params] = useSearchParams();
  const { state } = useLocation();
  const { me } = useSession();
  const scope = useTopicScope();
  const [q, setQuery] = useState("");
  const [pages, setPages] = useState<string[]>([]);
  const [selected, setSelected] = useState<ReadonlyMap<string, Attachment>>(new Map());
  const [confirmation, setConfirmation] = useState<Attachment[]>([]);
  const [report, setReport] = useState<{ count: number; failures: Map<string, { item: Attachment; reason: string }> }>();
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [publish, setPublish] = useState<Attachment>();
  const records = useRecords("attachment", name);
  const id = params.get("snapshot");
  const base = params.get("base");
  const path: SnapshotLink = (id, base) => ({ to: attachmentPath(name, params, kind, id, base), state });
  const back = directoryParams(params);
  const mayManage = (a: Attachment) => !a.deletion && (a.owner_id === me.id || me.role === "admin");
  const eligible = [...selected.values()].filter(mayManage);
  const refresh = () => setRevision((value) => value + 1);

  useEffect(() => {
    if (publish) document.getElementById("attachment-publish")?.scrollIntoView({ block: "start" });
  }, [publish]);

  const toggle = (item: Attachment) => setSelected((previous) => {
    const next = new Map(previous);
    if (!next.delete(item.id)) next.set(item.id, item);
    return next;
  });

  const recycle = async () => {
    setBusy(true);
    try {
      const chosen = new Map(confirmation.map((item) => [item.id, item]));
      const { completed, failures } = await bulkRecycle("/attachments/bulk-delete", [...chosen.keys()]);
      const failedItems = failures.flatMap(({ id, reason }) => {
        const item = chosen.get(id);
        return item ? [{ item, reason }] : [];
      });
      setReport((current) => {
        const remaining = new Map(current?.failures);
        for (const id of completed) remaining.delete(id);
        for (const failure of failedItems) remaining.set(failure.item.id, failure);
        return { count: completed.size, failures: remaining };
      });
      setSelected((current) => {
        const next = new Map(current);
        for (const id of completed) next.delete(id);
        for (const { item } of failedItems) next.set(item.id, item);
        return next;
      });
      setConfirmation([]);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-4">
      <PageTitle title={name} back={{ to: `/attachments?${back}#attachment-${name}`, label: t("attachments.directory.back"), state }}>
        <Pill tone="neutral">{t(`attachments.kind.${kind}`)}</Pill>
      </PageTitle>
      <TopicDirectory search={q} onSearch={(value) => { setQuery(value); setPages([]); }}
        searchLabel={t("attachments.detail.search")} resetOnTopicChange={["page"]} />
      {id && <SelectedSnapshot key={id} id={id} name={name} kind={kind} base={base} revision={revision} path={path}
        mayManage={mayManage} recycling={busy} onPublish={setPublish} onRecycle={setConfirmation} onChanged={refresh} />}
      {!!confirmation.length && (
        <RecycleConfirmation name={name} items={confirmation} busy={busy}
          onCancel={() => setConfirmation([])} onConfirm={() => void recycle()} />
      )}
      {report && (
        <Alert role="status">
          <AlertTitle>{t("attachments.recycle.result", { count: report.count })}</AlertTitle>
          {!!report.failures.size && (
            <AlertDescription>
              <ul className="grid gap-1">
                {[...report.failures.values()].map(({ item, reason }) => (
                  <li key={item.id} className="flex flex-wrap items-center gap-2">
                    <span><code title={item.id}>{snapshotName(item)}</code> · {reason}</span>
                    <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirmation([item])}>
                      {t("attachments.recycle.retry")}
                    </Button>
                  </li>
                ))}
              </ul>
            </AlertDescription>
          )}
        </Alert>
      )}
      <Card>
        <CardHeader><CardTitle className="text-base">{t("attachments.detail.versions")}</CardTitle></CardHeader>
        <CardContent className="grid gap-3">
          {!!selected.size && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span role="status">{t("attachments.recycle.selected", { count: selected.size })}</span>
                {!!eligible.length && (
                  <Button size="sm" variant="outline" disabled={busy}
                    onClick={() => setConfirmation(eligible)}>
                    {t("attachments.recycle.selectedAction", { count: eligible.length })}
                  </Button>
                )}
                <Button size="sm" variant="ghost" onClick={() => setSelected(new Map())}>
                  {t("attachments.recycle.clearSelection")}
                </Button>
              </div>
              {eligible.length < selected.size && <p className="text-xs text-muted-foreground">{t("attachments.recycle.ownerOnly")}</p>}
              <TopicMembershipActions kind="attachment_snapshot" ids={new Set(selected.keys())} onChanged={() => {
                setSelected(new Map());
                refresh();
              }} />
            </>
          )}
          <VersionResults key={JSON.stringify([q.trim(), pages])} name={name} kind={kind} topicId={scope.topicId} q={q.trim()}
            pages={pages} revision={revision} selected={selected} focused={id} path={path} onToggle={toggle}
            onPage={(cursor) => setPages((previous) => cursor ? [...previous, cursor] : previous.slice(0, -1))} />
        </CardContent>
      </Card>
      <WorkflowRecords targetType="attachment" targetId={name} records={records} anchor={false} />
      {publish && <AttachmentPublish key={publish.id} kind={kind} parent={publish} onPublished={refresh} />}
    </div>
  );
}

export function AttachmentDetailPage() {
  const { name = "" } = useParams();
  const [params] = useSearchParams();
  const { topicId } = useTopicScope();
  const kind = attachmentKind(params);
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [name]);
  return <AttachmentDetail key={JSON.stringify([name, kind, topicId])} name={name} kind={kind} />;
}
