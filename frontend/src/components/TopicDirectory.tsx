import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ErrorAlert, SelectField, TextField } from "@/components/common";
import { t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import { useCursorList, useSession } from "@/lib/hooks";
import { useTopicScope } from "@/lib/topics";
import type { TopicReferenceKind } from "@/lib/types";

export const DIRECTORY_PAGE_SIZE = 25;
const EMPTY_SELECTION: ReadonlySet<string> = new Set();

export function useDirectorySelection(key: string) {
  const [choice, setChoice] = useState<{ key: string; ids: ReadonlySet<string> }>({
    key, ids: EMPTY_SELECTION,
  });
  const selected = choice.key === key ? choice.ids : EMPTY_SELECTION;
  const toggle = (id: string) => {
    const copy = new Set(selected);
    if (!copy.delete(id)) copy.add(id);
    setChoice({ key, ids: copy });
  };
  const setSelected = (ids: ReadonlySet<string>) => setChoice({ key, ids });
  return { selected, setSelected, toggle };
}

export function DirectoryCheck({ name, checked, onChange }: {
  name: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <input
      type="checkbox"
      aria-label={t("topics.selectObject", { name })}
      checked={checked}
      onChange={onChange}
    />
  );
}

export function MoreItems({ page }: {
  page: { cursor?: string | null; loading: boolean; more: () => unknown };
}) {
  return page.cursor ? (
    <div>
      <Button size="sm" variant="outline" disabled={page.loading} onClick={() => void page.more()}>
        {t("topics.more")}
      </Button>
    </div>
  ) : null;
}

function TopicPicker({ value, onChange, all }: {
  value: string;
  onChange: (value: string) => void;
  all?: boolean;
}) {
  const id = useId();
  const scope = useTopicScope();
  const [showSearch, setShowSearch] = useState(false);
  const [q, setQuery] = useState("");
  const page = useCursorList(
    (cursor) => api.get("/topics", { query: { cursor, limit: 50, q: q.trim() || undefined } }),
    q.trim(),
  );
  const options = page.data ?? [];
  return (
    <div className="flex flex-wrap items-end gap-2">
      <SelectField
        id={id}
        label={all ? t("topics.scope") : t("topics.destination")}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{all ? t("topics.all") : t("topics.choose")}</option>
        {value && !options.some((topic) => topic.id === value) && (
          <option value={value}>{scope.topic?.id === value ? scope.topic.name : value}</option>
        )}
        {options.map((topic) => <option key={topic.id} value={topic.id}>{topic.name}</option>)}
      </SelectField>
      {showSearch && (
        <TextField
          id={`${id}-search`}
          label={t("topics.searchTopics")}
          value={q}
          onChange={(e) => setQuery(e.target.value)}
        />
      )}
      {page.cursor && !showSearch && (
        <Button size="sm" variant="ghost" onClick={() => setShowSearch(true)}>
          {t("topics.searchTopics")}
        </Button>
      )}
      <MoreItems page={page} />
      <ErrorAlert error={page.error} />
    </div>
  );
}

export function TopicDirectory({ search, onSearch, children }: {
  search: string;
  onSearch: (q: string) => void;
  children?: ReactNode;
}) {
  const id = useId();
  const scope = useTopicScope();
  return (
    <div className="mb-4 flex flex-wrap items-end gap-3">
      <TopicPicker all value={scope.topicId ?? ""} onChange={scope.choose} />
      <TextField id={id} label={t("topics.searchObjects")} value={search} onChange={(e) => onSearch(e.target.value)} />
      {children}
      {scope.topicId && (
        <Button size="sm" variant="link" asChild>
          <Link to={`/topics/${scope.topicId}`}>{t("topics.overview")}</Link>
        </Button>
      )}
    </div>
  );
}

export function TopicMembershipActions({ kind, ids, onChanged }: {
  kind: TopicReferenceKind;
  ids: ReadonlySet<string>;
  onChanged: () => void | Promise<void>;
}) {
  const { me } = useSession();
  const scope = useTopicScope();
  const [adding, setAdding] = useState(false);
  const [destination, setDestination] = useState("");
  const [busy, setBusy] = useState(false);
  if (me.pat_permissions && !me.pat_permissions.includes("experiment")) return null;
  const disabled = busy || ids.size === 0 || ids.size > 200;

  const change = async (topicId: string, action: "add" | "remove") => {
    setBusy(true);
    try {
      await api.post("/topics/{topic_id}/references", {
        path: { topic_id: topicId },
        body: { [action]: [...ids].map((id) => ({ kind, id })) },
      });
      toast.success(t(action === "add" ? "topics.added" : "topics.removed"));
      setAdding(false);
      setDestination("");
      await onChanged();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  if (ids.size === 0) return null;
  return (
    <div className="mb-3 flex flex-wrap items-end gap-2">
      {ids.size > 200 && <p className="text-sm text-muted-foreground">{t("topics.selectionLimit")}</p>}
      {adding ? (
        <>
          <TopicPicker value={destination} onChange={setDestination} />
          <Button size="sm" disabled={disabled || !destination} onClick={() => void change(destination, "add")}>
            {t("topics.add")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>{t("topics.cancel")}</Button>
        </>
      ) : (
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => setAdding(true)}>
          {t("topics.addSelected", { count: ids.size })}
        </Button>
      )}
      {scope.topic?.allowed_actions.includes("manage_references") && (
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => void change(scope.topic!.id, "remove")}>
          {t("topics.removeSelected", { count: ids.size })}
        </Button>
      )}
    </div>
  );
}

export function TopicSubmission({ topicId, checked, onChange, disabled }: {
  topicId: string | null;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  const scope = useTopicScope();
  if (!topicId) return null;
  const topic = scope.topic?.id === topicId ? scope.topic : null;
  return (
    <div className="grid gap-1 text-sm">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
        />
        {t("topics.submitTo", { name: topic?.name ?? topicId })}
      </label>
      {checked && !!scope.error && <ErrorAlert error={scope.error} />}
      {checked && topic?.deletion && <p className="text-muted-foreground">{t("topics.recycled")}</p>}
    </div>
  );
}
