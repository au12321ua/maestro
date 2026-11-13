# 打包与发布

导航：[协作流程](development-workflow.md) · [开发验证](testing.md) · [部署维护](deployment.md)

维护者用 `tools/release.py` 准备、核查和续跑发布。用户只需说明“把 dev 的 `<commit>` 发布为 `<X.Y.Z>`”；Agent 负责下面的步骤及相应验收。发布不会更新服务器，部署单独执行。

## 版本号规则

版本号 `X.Y.Z` 按使用者看到的变化决定，与代码内部改了多少无关（U-103）：

| 变化 | 加哪一位 | 例子 |
| --- | --- | --- |
| 只修问题，用法不变 | Z | 修复页面打不开、安装失败 |
| 新功能，旧用法照常可用 | Y，Z 归零 | 新的网页功能、新的 SDK 方法 |
| 不兼容：旧用法不再能用，或更新后要额外处理 | X，Y、Z 归零 | API 或 SDK 改名删字段、state 要重新准备 |

在第一个面向外部用户的版本（1.0.0）之前不承诺兼容，X 保持 0，不兼容的改动只加 Y，并在发布说明的“破坏性变更”小节写明。看不出算哪一类时取更高的一位。内部重构如果使用者感觉不到，只加 Z。

dev 上的 `.dev0` 只是占位，不预告下一版本号；发布时按这次包含的变化定号。一批功能合入 `dev` 并在实例上验证过就发一个正式版，不让 dev 长期积压未发布的功能：正式版本号与发布说明是“这一版有什么”的依据，`dev-` 预览只用于试用。

## 版本、tag 与发布物

根 `pyproject.toml` 是唯一权威版本。服务运行时从安装元数据读取版本；SDK 的包声明由 `tools/version.py` 同步，CI 检查漂移，SDK 独立安装。该命令还更新锁文件、OpenAPI、SDK 模型与契约摘要、网页类型。摘要仍覆盖整个 OpenAPI，版本变化也需要生成。Runner 的版本独立，不跟随服务；网页没有独立产品版本。同一版本号的开发预览靠构建名区分：发布包名与 `RELEASE.json` 的 `build` 是 `<版本>-<提交前 12 位>`，安装后的服务在 capabilities 的 `service_build` 和管理页的“服务版本”里报告它，从源码运行时只显示版本。

`vX.Y.Z` 是指向最终发布提交的附注 tag，不会修改源码中的版本。正式源码必须已经是 `X.Y.Z`，完整检查也必须针对这一个 SHA。共享后 tag 固定，Release 与包仓库的同版本文件也固定：缺少的补传，摘要相同的跳过，摘要不同的停止；不通过删 tag 或替换附件修复内容错误，应发新版本。

构建工具及其依赖由 `tools/build-constraints.txt` 固定，安装依赖由两个 `uv.lock` 与 `frontend/package-lock.json` 固定。wheel 使用提交时间，tar/gzip 按固定顺序、时间、权限与所有者归档；每次执行时间留在 Actions 中。`RELEASE.json` 保存源码 SHA、版本、数据库结构、Runner 修订和文件摘要。正式产物的构建环境是 Linux、Python 3.12、Node 24 与uv 0.11.31；两端构建相同 tag 并核对附件摘要。

## 准备可审阅的候选

先取得本次变更需要的验收和发布说明。说明应列出用户可见变化、破坏性变更、迁移要求、实际验证与未覆盖范围；在公开前完成，不能只留提交列表后再补写。破坏性变更与更新前后要做的事（例如重新首次准备）各放在标题含“破坏性变更”“更新后”等字样的小节里：管理页“更新”把这些小节排在发布说明前面。源码 CI 不代表真实 Docker、Hub、模型、系统安装等验收全部完成。

从干净的开发检出执行，原工作区不切分支；路径按本次任务选择：

```bash
git fetch origin
python3 tools/release.py prepare --commit <完整dev提交> --version X.Y.Z \
  --notes /tmp/release-notes.md --worktree /tmp/maestro-release-X.Y.Z
```

命令核对所选提交属于远端 dev、master 可以安全快进、两端没有已用 tag，然后创建两条短期分支及独立 worktree：

| 分支 | 内容 |
| --- | --- |
| `release/vX.Y.Z` | 在所选业务提交上增加正式版本、生成物和已审阅说明 `docs/releases/vX.Y.Z.md`；这是需要验证和打 tag 的最终 SHA |
| `release/vX.Y.Z-next` | 包含正式候选，再增加下一开发版本与生成物；worktree 停在这里，供提前审阅收尾差异 |

下一版本默认是下一补丁的 `.dev0`，可用 `--next-version X.Y.Z.dev0` 明确指定，必须晚于正式版本。所选业务 SHA 与正式候选 SHA 会同时输出，dev 后续推进不会暗中扩大候选。失败时保留 worktree；修复下载环境后重复相同 `prepare` 命令，依据已有提交继续生成。改变来源、下一版本、说明或出现范围外修改会停止，保留现场供核对，不重置已有文件。准备需要 uv、npm 及下载依赖的网络环境。

审阅两个提交、发布说明和该次验收范围后，推送准备的分支：

```bash
git diff <业务SHA> release/vX.Y.Z
git diff release/vX.Y.Z release/vX.Y.Z-next
git push origin release/vX.Y.Z release/vX.Y.Z-next
```

分支 push 自动运行完整检查。若新分支没有触发运行、提交只有文档，或需要重新验证，在 Actions 页手动对候选分支运行 `verify`，也可执行下列补验入口。它不会发布正式包：

```bash
python3 tools/release.py verify --version X.Y.Z
python3 tools/release.py status --version X.Y.Z
```

`verify/status/publish` 读取环境中的 `GITEA_API`（`<server>/api/v1`）、`GITEA_REPO`（`<owner>/<repo>`）、`GITEA_TOKEN`、`GITHUB_REPOSITORY` 与 `GH_TOKEN`。使用现有安全凭据来源注入，勿写入命令记录、说明或仓库。查询需读取 Actions、Release 和包仓库；补验与 `--retry` 还需调用 Actions dispatch/rerun。Git 推送沿用远端自己的身份。默认远端为 `origin`、`github`，可以通过命令参数指定。

## 发布与续跑

最终候选成功、差异与发布说明已经审阅，并完成本次其他验收后执行：

```bash
python3 tools/release.py publish --version X.Y.Z \
  --finish-worktree /tmp/maestro-release-X.Y.Z-finish
```

入口按以下事实推进，缺一步都不报告完成：

1. 从本仓库 `verify.yml` 的分支 push 或手动运行中，找到准确 SHA 的 `verify` 任务及所有必需检查步骤成功记录，打印原运行链接。旧 SHA、其他工作流、失败或跳过的检查不能代用；上传失败不影响独立的源码检查结论。
2. 核对源码版本、下一开发版本、发布说明与两端 tag；用普通快进推送推进 Gitea master，再创建、推送附注 tag。master 并发变化无法安全快进时停止，不强推或额外引入其他改动。
3. Gitea 的 tag 任务重新核对上述源码检查、tag 类型与 master 祖先关系（tag 类型向服务器查询：CI 的 checkout 会把本地附注 tag 换成普通引用），然后自行构建、校验 manifest/摘要，在临时 HOME 和全新环境中安装实际 wheel。检查服务、SDK 元数据、导入路径、必要入口、包内页面及 SDK 附件一致性，不操作已有 state。SDK 先发布到包仓库，Release 附件齐全后才公开草稿。
4. 等 Gitea 成功后向 GitHub 推送相同 master 和 tag。GitHub 从同一 tag 重建、校验、通过草稿补齐 Release，再单独重建 Pages 索引，不重复源码测试。
5. 核对两端附件实际下载摘要、SDK 包仓库和 Pages 索引的 wheel 摘要，再将正式准备及下一开发版本一起快进推到 dev。dev 有后续提交时，在 `--finish-worktree` 中合并保留它们；冲突保留现场，解决并提交后重跑。远端 dev 的头不会短暂停在正式版本。

默认等待 30 分钟，`--timeout` 可调整。超时或网络失败后重复同一命令继续。上传或 Pages job 失败时先检查日志、修复环境/权限，再加 `--retry` 只重跑失败任务；源码检查不会因此重复。Pages 失败只补索引，dev 收尾失败只续收尾。状态来自 Git、Actions、Releases、包仓库与 Pages，无额外发布状态库。自定义 Pages 域名用 `--pages-url` 指定 SDK 项目索引页。

若换机器续跑，先 fetch 两条准备分支及 tag；本地与远端准备分支不同会停止，须先核对。源码或正式文件确需修改时，应准备新候选、新版本。准备与收尾 worktree 和短期分支按协作流程核对后清理，不自动删除可能含冲突或草稿的目录。

## CI 分工

| 事件 | 检查与发布 |
| --- | --- |
| 任意分支 push | `verify` 完整检查；纯 Markdown 例外保留，`skills/` 与 `sdk/README.md` 仍检查 |
| PR 创建/更新 | 不再触发另一轮测试，使用本仓库 head 提交的 push 结果；不在宿主 runner 自动执行外部 fork |
| 合并产生新 SHA | 按 push 检查新提交；旧结果不能代用 |
| dev 检查通过 | 独立 `publish` 任务构建、验证、上传预览版 |
| v* tag | 跳过源码任务，`publish` 读取准确 SHA 的完整检查结果并验证实际交付物 |
| 手动 verify | 完整检查明确候选，不自动发布 |

直接 push 的检查发生在更新之后，失败不会撤销分支。实施时核实的 Gitea 1.27.1 未配置分支保护；合入前仍人工核对固定候选与最新目标，不能声称 CI 阻止了直接推送。需要改变保护规则时另行配置，并保留文档免检边界。发布任务按 ref 串行，构建环境独立初始化，不依赖跨任务 artifact 传输。同一 SHA 跨分支偶尔重复检查可以接受。

## 开发预览与 SDK 包仓库

dev 使用 `.dev0` 基础版本，Gitea Release 为 `dev-<SHA前12位>`；SDK 在构建时暂用 `.dev<CI运行号>`，例如 `0.4.1.dev1234`。重跑同一运行沿用原号，工作区声明会恢复，Release 附带的也是同一个 SDK wheel。预览 Release 和 `.devN` 包各保留最近 10 个；正式版不参与清理。

SDK 上传地址是 `<Gitea>/api/packages/<所有者>/pypi`，读取索引是同路径下的 `/simple`。正式 SDK 版本与源码相同。仓库 secret `PACKAGE_TOKEN` 只给上传步骤，使用具有所有者包写权限、`write:package` scope 的账号；不假定 job token 能写所有者包。缺失时预览警告并跳过包上传，正式发布失败；补齐后重跑发布任务即可。读取私有包需要 `read:package` 权限。用户安装入口仍见 [SDK 说明](../../sdk/README.md)。

## GitHub Pages 索引

GitHub 不提供 Python 包仓库，`maestro-sdk` 的正式版通过 Pages 的 PEP 503 索引分发。`index` job 从全部公开正式 Release 下载 SDK wheel，生成带 SHA-256 的链接，地址为 `https://<所有者>.github.io/<仓库>/simple`。旧版本保留，早期没有 SDK wheel 的 Release 跳过，开发预览不进入索引。

首次发布前在 GitHub Settings → Pages 将 Source 设为 GitHub Actions。Release 与 index 分成两个任务，Pages 失败不撤销 Release，可单独重跑 index；自带 `GITHUB_TOKEN` 读取 Release，OIDC 部署 Pages。发布后实际部署及数据库升级/恢复继续按[部署维护](deployment.md)执行，代码回退不代表 state 可以回退。
