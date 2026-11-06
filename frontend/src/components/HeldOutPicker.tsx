import { t } from "@/i18n";
import { Button } from "@/components/ui/button";
import type { Task } from "@/lib/types";

export function HeldOutPicker({
  tasks,
  selected,
  onChange,
}: {
  tasks: Task[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  return (
    <fieldset className="grid gap-2 text-sm">
      <legend>{t("comparison.heldOut.choose")}</legend>
      <p className="text-xs text-muted-foreground">
        {t("comparison.heldOut.fixedHint")}
      </p>
      {selected.some((id) => !tasks.some((task) => task.id === id)) && (
        <p>{t("comparison.heldOut.wrongVersion")}</p>
      )}
      <Button
        type="button"
        size="sm"
        variant="ghost"
        onClick={() => onChange([])}
      >
        {t("comparison.heldOut.clear")}
      </Button>
      <div className="grid max-h-48 gap-1 overflow-auto">
        {tasks.map((task) => (
          <label key={task.id} className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={selected.includes(task.id)}
              onChange={(e) =>
                onChange(
                  e.target.checked
                    ? [...selected, task.id]
                    : selected.filter((id) => id !== task.id),
                )
              }
            />
            {task.task_key}
          </label>
        ))}
      </div>
    </fieldset>
  );
}
