// A frozen comparison (06 section 8): its numbers never change; whether each run's files
// still open is checked now. Notes are their authors', apart from the computed table.
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ComparisonView } from "@/components/ComparisonView";
import {
  ErrorAlert,
  Loading,
  PageTitle,
  TextAreaField,
} from "@/components/common";
import { api, errorText } from "@/lib/api";
import { DIMENSION, SCENARIO } from "@/lib/comparison";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { Dimension, Snapshot, SnapshotNote } from "@/lib/types";

export function SnapshotPage() {
  const { snapshotId = "" } = useParams();
  const navigate = useNavigate();
  const path = { snapshot_id: snapshotId };
  const snapshot = useLoad(
    async () =>
      (await api.get("/analysis-snapshots/{snapshot_id}", { path })).data,
    [snapshotId],
  );
  const s = snapshot.data;
  if (!s)
    return snapshot.error ? <ErrorAlert error={snapshot.error} /> : <Loading />;

  const remove = async () => {
    if (!window.confirm("删除这份快照？它会进回收站。")) return;
    try {
      await api.del("/analysis-snapshots/{snapshot_id}", { path });
      toast.success("已移入回收站");
      navigate(
        s.collection_id ? `/collections/${s.collection_id}` : "/collections",
      );
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    }
  };

  return (
    <div className="grid gap-4">
      <PageTitle title={`快照：${s.name}`}>
        {s.allowed_actions.includes("delete") && !s.deletion && (
          <Button size="sm" variant="outline" onClick={() => void remove()}>
            <Trash2 />
            删除
          </Button>
        )}
      </PageTitle>
      <Card>
        <CardContent className="grid gap-1 pt-6 text-sm">
          <p>
            {s.owner_username}
            {s.pat_id ? "（PAT）" : ""} 保存于 {fmtTime(s.created_at)}，
            {s.run_count} 个运行。
            {s.collection_id && (
              <>
                {" "}
                来自
                <Link
                  className="ml-1 underline"
                  to={`/collections/${s.collection_id}`}
                >
                  集合
                </Link>
                。
              </>
            )}
          </p>
          {s.spec && (
            <p className="text-muted-foreground">
              当时的规格：{SCENARIO[s.spec.scenario].label}，可变：
              {s.spec.variable_dimensions
                .map((d) => DIMENSION[d as Dimension])
                .join("、") || "无"}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            数字在保存时已冻结，运行或日志之后被删除、过期都不改变它们；“证据”一列是现在的情况。
          </p>
        </CardContent>
      </Card>
      <Notes snapshot={s} onChange={() => void snapshot.reload()} />
      <ComparisonView
        table={s.result}
        evidence={s.evidence}
        axes={s.spec?.variable_dimensions as Dimension[] | undefined}
        csvName={`${s.name}.csv`}
      />
    </div>
  );
}

function Notes({
  snapshot,
  onChange,
}: {
  snapshot: Snapshot;
  onChange: () => void;
}) {
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<SnapshotNote | null>(null);
  const path = { snapshot_id: snapshot.id };
  const save = async () => {
    try {
      if (editing) {
        await api.put("/analysis-snapshots/{snapshot_id}/notes/{note_id}", {
          path: { ...path, note_id: editing.id },
          body: { body: text },
        });
      } else {
        await api.post("/analysis-snapshots/{snapshot_id}/notes", {
          path,
          body: { body: text },
        });
      }
      setText("");
      setEditing(null);
      onChange();
    } catch (err) {
      toast.error(`保存失败：${errorText(err)}`);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">结论</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <p className="text-xs text-muted-foreground">
          结论是人或 Agent
          写的看法，和平台算出的数字分开存放；只有作者能改自己的结论。
        </p>
        {snapshot.notes.length === 0 && (
          <p className="text-sm text-muted-foreground">还没有结论。</p>
        )}
        {snapshot.notes.map((n) => (
          <div key={n.id} className="rounded-md border p-3 text-sm">
            <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {n.author_username}
              {n.pat_id ? "（PAT）" : ""} · {fmtTime(n.updated_at)}
              {n.editable && (
                <button
                  type="button"
                  className="underline"
                  onClick={() => {
                    setEditing(n);
                    setText(n.body);
                  }}
                >
                  修改
                </button>
              )}
            </div>
            <p className="whitespace-pre-wrap break-words">{n.body}</p>
          </div>
        ))}
        {snapshot.allowed_actions.includes("note") && !snapshot.deletion && (
          <div className="grid gap-2">
            <TextAreaField
              id="snapshot-note-body"
              label={editing ? "修改结论" : "写结论"}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={!text.trim()}
                onClick={() => void save()}
              >
                保存
              </Button>
              {editing && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setEditing(null);
                    setText("");
                  }}
                >
                  取消
                </Button>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
