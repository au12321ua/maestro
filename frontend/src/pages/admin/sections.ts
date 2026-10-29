// The administration sections: their URLs (/admin/<id>) and names in the sidebar.
import { t } from "@/i18n";

export const ADMIN_SECTIONS = [
  { id: "members", label: t("admin.sections.members") },
  { id: "credentials", label: t("admin.sections.credentials") },
  { id: "models", label: t("admin.sections.models") },
  { id: "harnesses", label: t("admin.sections.harnesses") },
  { id: "policy", label: t("admin.sections.policy") },
  { id: "capacity", label: t("admin.sections.capacity") },
  { id: "setup-network", label: t("admin.sections.setupNetwork") },
  { id: "datasets", label: t("admin.sections.datasets") },
  { id: "recycle-bin", label: t("admin.sections.recycleBin") },
  { id: "audit", label: t("admin.sections.audit") },
  { id: "maintenance", label: t("admin.sections.maintenance") },
] as const;

export type AdminSection = (typeof ADMIN_SECTIONS)[number]["id"];
