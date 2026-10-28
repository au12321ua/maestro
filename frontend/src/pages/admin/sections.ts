// The administration sections: their URLs (/admin/<id>) and names in the sidebar.
export const ADMIN_SECTIONS = [
  { id: "members", label: "成员与邀请" },
  { id: "credentials", label: "凭据与主密钥" },
  { id: "models", label: "模型" },
  { id: "harnesses", label: "Harness" },
  { id: "policy", label: "团队策略" },
  { id: "capacity", label: "容量" },
  { id: "setup-network", label: "安装网络" },
  { id: "datasets", label: "数据集导入" },
  { id: "recycle-bin", label: "回收站" },
  { id: "audit", label: "审计" },
  { id: "maintenance", label: "维护" },
] as const;

export type AdminSection = (typeof ADMIN_SECTIONS)[number]["id"];
