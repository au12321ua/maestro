import { useEffect, useState } from "react";
import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import { AttachmentPagination } from "@/components/AttachmentPagination";
import { AttachmentPublish } from "@/components/AttachmentPublish";
import { DIRECTORY_PAGE_SIZE, TopicDirectory } from "@/components/TopicDirectory";
import { EmptyNote, ErrorAlert, Loading, PageTitle, SelectField } from "@/components/common";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/lib/api";
import { attachmentKind, attachmentPath, directoryCursor, directoryParams, type AttachmentDirectoryNavigation, type AttachmentKind } from "@/lib/attachment-directory";
import { fmtTime, snapshotName } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import { useTopicScope } from "@/lib/topics";
import { t } from "@/i18n";

function LegacyAttachmentLink({ id, params }: { id: string; params: URLSearchParams }) {
  const { topicId } = useTopicScope();
  const { state } = useLocation();
  const { data, error, loading } = useLoad(async () => (
    await api.get("/attachment-directory/version", { query: { snapshot_id: id } })
  ).data);
  const next = directoryParams(params);
  next.set("topic", topicId ?? "");
  if (data) {
    const { snapshot } = data;
    return <Navigate replace state={state} to={attachmentPath(snapshot.name, next, snapshot.kind ?? "attachment", id, params.get("snapshot") ? params.get("base") ?? undefined : undefined)} />;
  }
  return (
    <div>
      <PageTitle title={t("attachments.title")} back={{ to: `/attachments?${next}`, label: t("attachments.directory.back") }} />
      {loading && <Loading />}
      <ErrorAlert error={error} />
    </div>
  );
}

function DirectoryResults({ kind, params, cursor, page, revision, onPage }: {
  kind: AttachmentKind;
  params: URLSearchParams;
  cursor: string | null;
  page?: number;
  revision: number;
  onPage: (cursor?: string) => void;
}) {
  const { topicId } = useTopicScope();
  const { hash, state } = useLocation();
  const { data, loading, error } = useLoad(() => api.get("/attachment-directory", {
    query: { kind, topic_id: topicId, q: params.get("q")?.trim(), cursor, limit: DIRECTORY_PAGE_SIZE },
  }), [revision]);
  const next = new URLSearchParams(params);
  next.set("topic", topicId ?? "");

  useEffect(() => {
    if (hash && data) document.getElementById(hash.slice(1))?.scrollIntoView({ block: "center" });
  }, [hash, data]);

  return (
    <>
      {loading && !data && <Loading />}
      <ErrorAlert error={error} />
      {data?.data.length === 0 && <EmptyNote>{t("attachments.list.empty")}</EmptyNote>}
      {!!data?.data.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("attachments.directory.name")}</TableHead>
              <TableHead>{t("attachments.directory.latest")}</TableHead>
              <TableHead>{t("attachments.directory.published")}</TableHead>
              <TableHead>{t("attachments.directory.snapshots")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.data.map(({ name, latest, match, snapshot_count: count }) => (
              <TableRow key={name} id={`attachment-${name}`}>
                <TableCell>
                  <Link state={state} to={attachmentPath(name, next, kind)} className="font-medium text-primary hover:underline">
                    {name}
                  </Link>
                  {match && match.id !== latest.id && (
                    <Link state={state} className="block text-xs text-primary hover:underline" to={attachmentPath(name, next, kind, match.id)}>
                      {t("attachments.directory.match", { snapshot: snapshotName(match) })}
                    </Link>
                  )}
                </TableCell>
                <TableCell>
                  <Link state={state} to={attachmentPath(name, next, kind, latest.id)} className="text-primary hover:underline">
                    {snapshotName(latest)}
                  </Link>
                  {latest.description && (
                    <p className="line-clamp-2 max-w-sm text-xs text-muted-foreground" title={latest.description}>
                      {latest.description}
                    </p>
                  )}
                </TableCell>
                <TableCell className="whitespace-nowrap text-xs">{fmtTime(latest.created_at)}</TableCell>
                <TableCell className="tabular-nums">{count}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <AttachmentPagination page={page} next={data?.meta.next_cursor} loading={loading}
        onPrevious={() => onPage()} onNext={onPage} />
    </>
  );
}

export function AttachmentsPage() {
  const [params, setParams] = useSearchParams();
  const { state } = useLocation();
  const { topicId } = useTopicScope();
  const [revision, setRevision] = useState(0);
  const snapshot = params.get("snapshot") || params.get("base");
  const name = params.get("name");
  const kind = attachmentKind(params);
  const cursor = directoryCursor(params);
  const previous: AttachmentDirectoryNavigation["previousAttachmentPages"] =
    cursor ? state?.previousAttachmentPages ?? [] : [];
  const page = cursor ? (previous[0] === null ? previous.length + 1 : undefined) : 1;

  if (snapshot) return <LegacyAttachmentLink key={snapshot} id={snapshot} params={params} />;
  if (name) return <Navigate replace state={state} to={attachmentPath(name, params, kind)} />;

  const update = (field: "kind" | "q", value: string) => {
    const next = new URLSearchParams(params);
    next.set(field, value);
    next.delete("page");
    setParams(next, { replace: true });
  };
  const onPage = (nextCursor?: string) => {
    const next = new URLSearchParams(params);
    const destination = nextCursor ?? previous.at(-1);
    if (destination) next.set("page", destination);
    else next.delete("page");
    const state: AttachmentDirectoryNavigation = {
      previousAttachmentPages: nextCursor ? [...previous, cursor] : previous.slice(0, -1),
    };
    setParams(next, { state });
  };
  const pageKey = JSON.stringify([topicId, kind, params.get("q")?.trim(), cursor]);

  return (
    <div className="grid gap-4">
      <PageTitle title={t("attachments.title")} />
      <TopicDirectory search={params.get("q") ?? ""} onSearch={(value) => update("q", value)}
        searchLabel={t("attachments.directory.search")} resetOnTopicChange={["page"]}>
        <SelectField id="package-kind" label={t("attachments.kind.label")} value={kind}
          onChange={(event) => update("kind", event.target.value)}>
          <option value="attachment">{t("attachments.kind.attachment")}</option>
          <option value="submission">{t("attachments.kind.submission")}</option>
        </SelectField>
      </TopicDirectory>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("attachments.list.title")}</CardTitle>
          <CardDescription>{t("attachments.list.description")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          <DirectoryResults key={pageKey} kind={kind} params={params} cursor={cursor} page={page} revision={revision} onPage={onPage} />
        </CardContent>
      </Card>
      <AttachmentPublish key={kind} kind={kind} onPublished={() => setRevision((value) => value + 1)} />
    </div>
  );
}
