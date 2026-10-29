// Language choice (N-18): stored in this browser, applied by reloading. The English text is
// AI-translated and unreviewed, and says so while it is shown (U-58).
import { Languages } from "lucide-react";
import { LANGUAGES, language, setLanguage, t, type Language } from "@/i18n";
import { cn } from "@/lib/utils";

export function LanguageSwitch({ className }: { className?: string }) {
  const notice = t("common.language.aiNotice");
  return (
    <div className={cn("grid gap-1 text-xs", className)}>
      <label className="flex items-center gap-1.5">
        <Languages className="size-3.5 shrink-0" aria-hidden />
        <span className="sr-only">{t("common.language.label")}</span>
        <select
          aria-label={t("common.language.label")}
          className="rounded border border-current/30 bg-transparent px-1 py-0.5 text-xs"
          value={language}
          onChange={(e) => setLanguage(e.target.value as Language)}
        >
          {LANGUAGES.map((option) => (
            <option key={option} value={option} className="text-black">
              {t(`common.language.${option}`)}
            </option>
          ))}
        </select>
      </label>
      {notice && (
        <p data-testid="ai-translation-notice" className="opacity-70">
          {notice}
        </p>
      )}
    </div>
  );
}
