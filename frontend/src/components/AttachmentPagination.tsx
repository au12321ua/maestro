import { Button } from "@/components/ui/button";
import { t } from "@/i18n";

export function AttachmentPagination({ page, next, loading, onPrevious, onNext }: {
  page?: number;
  next?: string | null;
  loading: boolean;
  onPrevious: () => void;
  onNext: (cursor: string) => void;
}) {
  if (page === 1 && !next) return null;
  return (
    <nav className="flex flex-wrap items-center gap-2" aria-label={t("attachments.pagination.label")}>
      <Button size="sm" variant="outline" disabled={page === 1} onClick={onPrevious}>
        {t("attachments.pagination.previous")}
      </Button>
      {page !== undefined && (
        <span className="text-xs tabular-nums text-muted-foreground">
          {t("attachments.pagination.page", { page })}
        </span>
      )}
      <Button size="sm" variant="outline" disabled={loading || !next} onClick={() => next && onNext(next)}>
        {t("attachments.pagination.next")}
      </Button>
    </nav>
  );
}
