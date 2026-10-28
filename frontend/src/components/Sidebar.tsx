// The navigation sidebar (U-45): groups of pages, the administration sections under "管理",
// the system state and the user at the bottom. It folds to icons on wide screens and is a
// drawer on narrow ones.
import { useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  ClipboardList,
  Database,
  LayoutDashboard,
  Layers,
  Library,
  Menu,
  PackageOpen,
  PanelLeftClose,
  PanelLeftOpen,
  Paperclip,
  Play,
  Settings,
  ShieldCheck,
  X,
  type LucideIcon,
} from "lucide-react";
import { useSession } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { ADMIN_SECTIONS } from "@/pages/admin/sections";

interface Link {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

const GROUPS: { title?: string; links: Link[] }[] = [
  { links: [{ to: "/", label: "工作台", icon: LayoutDashboard, end: true }] },
  {
    title: "实验",
    links: [
      { to: "/runs", label: "运行", icon: Play },
      { to: "/batches", label: "批次", icon: Layers },
      { to: "/collections", label: "集合", icon: Library },
      { to: "/plans", label: "方案", icon: ClipboardList },
    ],
  },
  {
    title: "资源",
    links: [
      { to: "/datasets", label: "数据集", icon: Database },
      { to: "/attachments", label: "附件", icon: Paperclip },
      { to: "/exports", label: "导出", icon: PackageOpen },
    ],
  },
  { links: [{ to: "/settings", label: "设置", icon: Settings }] },
];
const ADMIN: Link = { to: "/admin", label: "管理", icon: ShieldCheck };

/** Folded or not is a per-browser preference; storage may be unavailable. */
const FOLDED_KEY = "maestro.sidebar.folded";
function readFolded(): boolean {
  try {
    return localStorage.getItem(FOLDED_KEY) === "1";
  } catch {
    return false;
  }
}
function writeFolded(folded: boolean) {
  try {
    localStorage.setItem(FOLDED_KEY, folded ? "1" : "0");
  } catch {
    // the preference just does not survive a reload
  }
}

const ITEM = "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm";
const itemClass = ({ isActive }: { isActive: boolean }) =>
  cn(ITEM, isActive ? "bg-sidebar-accent text-white" : "hover:bg-sidebar-accent/60");

export function Sidebar({
  footer,
  collapsedFooter,
}: {
  footer: ReactNode;
  collapsedFooter: ReactNode;
}) {
  const { me } = useSession();
  const { pathname } = useLocation();
  const [folded, setFolded] = useState(readFolded);
  const [drawer, setDrawer] = useState(false);
  const fold = (next: boolean) => {
    setFolded(next);
    writeFolded(next);
  };
  const groups = me.role === "admin" ? [...GROUPS, { links: [ADMIN] }] : GROUPS;
  const inAdmin = pathname === "/admin" || pathname.startsWith("/admin/");

  const links = (narrow: boolean) => (
    <nav aria-label="主导航" className="grid gap-3">
      {groups.map((group, index) => (
        <div key={group.title ?? index} className="grid gap-0.5">
          {group.title && !narrow && (
            <div className="px-2.5 pb-0.5 text-xs text-sidebar-foreground/60">{group.title}</div>
          )}
          {group.links.map((link) => (
            <div key={link.to} className="grid gap-0.5">
              <NavLink
                to={link.to}
                end={link.end}
                title={narrow ? link.label : undefined}
                aria-label={link.label}
                className={(state) => cn(itemClass(state), narrow && "justify-center px-0")}
              >
                <link.icon className="size-4 shrink-0" />
                {!narrow && <span>{link.label}</span>}
              </NavLink>
              {link === ADMIN && inAdmin && !narrow && (
                <nav
                  aria-label="管理"
                  className="ml-4 grid gap-0.5 border-l border-sidebar-border pl-2"
                >
                  {ADMIN_SECTIONS.map((s) => (
                    <NavLink
                      key={s.id}
                      to={`/admin/${s.id}`}
                      className={(state) => cn(itemClass(state), "py-1 text-[13px]")}
                    >
                      {s.label}
                    </NavLink>
                  ))}
                </nav>
              )}
            </div>
          ))}
        </div>
      ))}
    </nav>
  );

  const brand = (
    <NavLink to="/" className="text-lg font-semibold tracking-wide text-sidebar-primary">
      Maestro
    </NavLink>
  );

  return (
    <>
      {/* Narrow screens: a bar with the menu button; the sidebar opens as a drawer. */}
      <div className="fixed inset-x-0 top-0 z-30 flex h-12 items-center gap-3 border-b bg-sidebar px-4 text-sidebar-foreground md:hidden">
        <button type="button" aria-label="打开导航" onClick={() => setDrawer(true)}>
          <Menu className="size-5" />
        </button>
        {brand}
      </div>
      <div className="h-12 shrink-0 md:hidden" aria-hidden />
      {drawer && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setDrawer(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-64 max-w-[85vw] flex-col gap-4 overflow-y-auto bg-sidebar p-3 text-sidebar-foreground">
            <div className="flex items-center justify-between px-1">
              {brand}
              <button type="button" aria-label="关闭导航" onClick={() => setDrawer(false)}>
                <X className="size-5" />
              </button>
            </div>
            {/* Following a link closes the drawer. */}
            <div onClick={(e) => (e.target as Element).closest("a") && setDrawer(false)}>
              {links(false)}
            </div>
            <div className="mt-auto grid gap-2 border-t border-sidebar-border pt-3">{footer}</div>
          </aside>
        </div>
      )}
      {/* Wide screens: always there, folded to icons or not; the column keeps the sidebar's
          colour down a page longer than the window. */}
      <div className={cn("hidden shrink-0 bg-sidebar md:block", folded ? "w-14" : "w-56")}>
        <aside className="sticky top-0 flex h-screen flex-col gap-4 overflow-y-auto p-3 text-sidebar-foreground">
          <div
            className={cn("flex items-center px-1", folded ? "justify-center" : "justify-between")}
          >
            {!folded && brand}
            <button
              type="button"
              aria-label={folded ? "展开侧边栏" : "收起侧边栏"}
              title={folded ? "展开侧边栏" : "收起侧边栏"}
              className="rounded p-1 hover:bg-sidebar-accent/60"
              onClick={() => fold(!folded)}
            >
              {folded ? (
                <PanelLeftOpen className="size-4" />
              ) : (
                <PanelLeftClose className="size-4" />
              )}
            </button>
          </div>
          {links(folded)}
          <div className="mt-auto grid gap-2 border-t border-sidebar-border pt-3">
            {folded ? collapsedFooter : footer}
          </div>
        </aside>
      </div>
    </>
  );
}
