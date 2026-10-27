# N-23 现场说明与证据进 Gitea 私有仓库

状态：已确认 · 优先级：一 · 2026-10-27 · 相关：U-35、`AGENTS.md`、[开发交接](../HANDOFF.md)、[开发流程](../guides/development-workflow.md)

## 问题

部署现场说明（`LOCAL_ENVIRONMENT.md`）、验证证据和开发草稿都放在主仓 ignored `.local/`，只存在于某一台机器的某一个 checkout 里。换一台机器或另一个人、另一个 Agent 开工时读不到，也没有版本记录；而以后开源时，这些内容又不能进公开仓库。

## 方案

按内容的需求分开放，全部用已有的自托管 Gitea，不另建服务：

| 内容 | 放在哪里 |
| --- | --- |
| 部署现场说明：主机、安装版本、访问方式、注意事项 | 私有仓库 `PhySpace/maestro-site`，每台主机一个文件，有版本、按 Gitea 权限访问。凭据不进仓库，只写凭据存放在哪里 |
| CI 产生的证据 | Gitea Actions artifacts，带过期时间 |
| 手动验证的证据（日志、截图、capture） | `maestro-site` 中按日期与提交组织的目录；大文件用 LFS 或 Release 附件。仍须先脱敏 |
| 提案讨论与任务 | 需要时每个提案开一个 Gitea Issue，Agent 可经 API 读写 |
| 本机草稿、临时笔记 | 继续放 ignored `.local/`，不承载需要共享的内容 |

放弃的做法：网络白板（没有版本与结构，Agent 难以稳定读写，U-14 已放弃过同类协调方式）；公网服务器上用令牌访问的文件夹（要自己处理令牌分发与轮换、HTTPS、备份，没有版本记录）。证据量以后超出 Gitea 的承受范围时，再考虑对象存储。

## 影响

- `AGENTS.md`、开发交接、开发流程与文档入口中关于 `.local/` 的写法改为上表（本提案确认时已同步修改）。
- `maestro-site` 由用户在 Gitea 上创建；建好后，把部署主机上现有的 `.local/LOCAL_ENVIRONMENT.md` 与仍有价值的证据索引迁进去，并在本提案移到“已完成”。

## 验收

- 另一台机器上的 Agent 只凭 Gitea 权限就能读到部署现场说明。
- 仓库与 `maestro-site` 中都没有凭据明文。

## 待定问题

- `maestro-site` 的目录结构与证据保留期限。
- 是否启用 LFS。
