// "Add to a collection" on a run page or for the runs selected in the list: they join if
// they match the collection's fixed values; otherwise the refusal says what differs.
import { useState } from "react";
import { Link } from "react-router-dom";
import { Library } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
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
      toast.error(`无法加载集合：${errorText(err)}`);
    }
  };
  const add = async () => {
    try {
      await api.post("/collections/{collection_id}/runs", {
        path: { collection_id: chosen },
        body: { runs: runIds.map((run_id) => ({ run_id })) },
      });
      toast.success(
        runIds.length > 1 ? `已把 ${runIds.length} 个运行加入集合` : "已加入集合",
      );
      setOpen(false);
    } catch (err) {
      toast.error(`没有加入：${errorText(err)}`, { duration: 10000 });
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
        加入集合
      </Button>
      {open && collections && collections.length === 0 && (
        <span className="text-xs text-muted-foreground">
          还没有集合，
          <Link className="underline" to="/collections">
            去新建
          </Link>
        </span>
      )}
      {open && collections && collections.length > 0 && (
        <>
          <select
            aria-label="选择集合"
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
            加入
          </Button>
        </>
      )}
    </span>
  );
}
