// "Add to a collection" on a run page or for the runs selected in the list: they join if
// they match the collection's fixed values; otherwise the refusal says what differs.
import { useState } from "react";
import { Link } from "react-router-dom";
import { Library } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Trans, t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import type { Collection } from "@/lib/types";

export function AddToCollection({ runIds }: { runIds: string[] }) {
  const [open, setOpen] = useState(false);
  const [collections, setCollections] = useState<Collection[] | null>(null);
  const [chosen, setChosen] = useState("");
  const show = async () => {
    setOpen(!open);
    if (collections) return;
    try {
      const found = (await api.get("/collections")).data.filter((c) =>
        c.allowed_actions.includes("add_runs"),
      );
      setCollections(found);
      setChosen(found[0]?.id ?? "");
    } catch (err) {
      toast.error(
        t("comparison.addToCollection.loadFailed", { error: errorText(err) }),
      );
    }
  };
  const add = async () => {
    try {
      await api.post("/collections/{collection_id}/runs", {
        path: { collection_id: chosen },
        body: { runs: runIds.map((run_id) => ({ run_id })) },
      });
      toast.success(
        runIds.length > 1
          ? t("comparison.addToCollection.addedMany", { count: runIds.length })
          : t("comparison.addToCollection.added"),
      );
      setOpen(false);
    } catch (err) {
      toast.error(
        t("comparison.addToCollection.notAdded", { error: errorText(err) }),
        { duration: 10000 },
      );
    }
  };
  return (
    <span className="flex items-center gap-1">
      <Button
        size="sm"
        variant="outline"
        disabled={runIds.length === 0}
        onClick={() => void show()}
      >
        <Library />
        {t("comparison.addToCollection.button")}
      </Button>
      {open && collections && collections.length === 0 && (
        <span className="text-xs text-muted-foreground">
          <Trans
            i18nKey="comparison.addToCollection.noneYet"
            components={{ link: <Link className="underline" to="/collections" /> }}
          />
        </span>
      )}
      {open && collections && collections.length > 0 && (
        <>
          <select
            aria-label={t("comparison.addToCollection.select")}
            className="h-8 max-w-[14rem] rounded-md border bg-background px-2 text-sm"
            value={chosen}
            onChange={(e) => setChosen(e.target.value)}
          >
            {collections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <Button size="sm" disabled={!chosen} onClick={() => void add()}>
            {t("comparison.addToCollection.add")}
          </Button>
        </>
      )}
    </span>
  );
}
