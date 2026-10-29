// Web UI languages (N-18). Every text shown in the interface lives in src/locales/<lang>/*.json;
// components call t("<file>.<key>"). The language is fixed for the page's lifetime: switching
// stores the choice and reloads, so labels computed at module load (lib/format.ts) are right.
import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import zhAdmin from "./locales/zh-CN/admin.json";
import zhAdminConfig from "./locales/zh-CN/adminConfig.json";
import zhAttachments from "./locales/zh-CN/attachments.json";
import zhAuth from "./locales/zh-CN/auth.json";
import zhCommon from "./locales/zh-CN/common.json";
import zhComparison from "./locales/zh-CN/comparison.json";
import zhDatasets from "./locales/zh-CN/datasets.json";
import zhErrors from "./locales/zh-CN/errors.json";
import zhExports from "./locales/zh-CN/exports.json";
import zhNewRun from "./locales/zh-CN/newRun.json";
import zhPlans from "./locales/zh-CN/plans.json";
import zhRuns from "./locales/zh-CN/runs.json";
import zhSettings from "./locales/zh-CN/settings.json";
import zhWorkbench from "./locales/zh-CN/workbench.json";
import enAdmin from "./locales/en/admin.json";
import enAdminConfig from "./locales/en/adminConfig.json";
import enAttachments from "./locales/en/attachments.json";
import enAuth from "./locales/en/auth.json";
import enCommon from "./locales/en/common.json";
import enComparison from "./locales/en/comparison.json";
import enDatasets from "./locales/en/datasets.json";
import enErrors from "./locales/en/errors.json";
import enExports from "./locales/en/exports.json";
import enNewRun from "./locales/en/newRun.json";
import enPlans from "./locales/en/plans.json";
import enRuns from "./locales/en/runs.json";
import enSettings from "./locales/en/settings.json";
import enWorkbench from "./locales/en/workbench.json";

const zhCN = {
  admin: zhAdmin,
  adminConfig: zhAdminConfig,
  attachments: zhAttachments,
  auth: zhAuth,
  common: zhCommon,
  comparison: zhComparison,
  datasets: zhDatasets,
  errors: zhErrors,
  exports: zhExports,
  newRun: zhNewRun,
  plans: zhPlans,
  runs: zhRuns,
  settings: zhSettings,
  workbench: zhWorkbench,
};

// Not typed as zhCN: English plural keys (key_one, key_other) differ; check:i18n compares them.
const en = {
  admin: enAdmin,
  adminConfig: enAdminConfig,
  attachments: enAttachments,
  auth: enAuth,
  common: enCommon,
  comparison: enComparison,
  datasets: enDatasets,
  errors: enErrors,
  exports: enExports,
  newRun: enNewRun,
  plans: enPlans,
  runs: enRuns,
  settings: enSettings,
  workbench: enWorkbench,
};

export const LANGUAGES = ["zh-CN", "en"] as const;
export type Language = (typeof LANGUAGES)[number];

const STORAGE_KEY = "maestro.language";

function stored(): Language | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return (LANGUAGES as readonly string[]).includes(value ?? "") ? (value as Language) : null;
  } catch {
    return null;
  }
}

/** The stored choice, else the browser's: Chinese for any zh-*, English otherwise (U-58). */
function detect(): Language {
  const choice = stored();
  if (choice) return choice;
  const preferred = navigator.languages?.length ? navigator.languages : [navigator.language];
  return preferred.some((tag) => tag?.toLowerCase().startsWith("zh")) ? "zh-CN" : "en";
}

export const language: Language = detect();

void i18next.use(initReactI18next).init({
  resources: { "zh-CN": { translation: zhCN }, en: { translation: en } },
  lng: language,
  fallbackLng: "en",
  initAsync: false,
  interpolation: { escapeValue: false },
  returnNull: false,
});
document.documentElement.lang = language;

export function setLanguage(next: Language) {
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Without storage the choice lasts until the next reload.
  }
  window.location.reload();
}

export const t = i18next.t.bind(i18next);

/** The current language's title for an API error code, or null when it has none. */
export function translateCode(code: string): string | null {
  const key = `errors.codes.${code}`;
  return i18next.exists(key) ? String(i18next.t(key as never)) : null;
}
export { Trans } from "react-i18next";

declare module "i18next" {
  interface CustomTypeOptions {
    resources: { translation: typeof zhCN };
    returnNull: false;
  }
}
