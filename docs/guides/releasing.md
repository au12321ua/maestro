# 打包与发布

导航：[协作流程](development-workflow.md) · [开发验证](testing.md) · [部署维护](deployment.md)

本页维护发布者的操作流程、版本规则与包仓库设置；使用者的安装和维护步骤见部署指南。

## 自动发布

推送到 `dev` 且通过后，同一个 job 的最后两步把发布包发成 Gitea 预发布（tag `dev-<提交前 12 位>`，保留最近 10 个），把 SDK 发到 Gitea 的 Python 包仓库（`<版本>.dev<CI 运行号>`，保留最近 10 个）；`master` 上的 `v*` tag 再完整跑一遍，发成正式版本。写令牌只交给这两步（发 Release 用 job 自带的令牌，发包用仓库 secret `PACKAGE_TOKEN`），它们只运行标准库写的上传脚本。GitHub 上的 `.github/workflows/release.yml` 只在推送 tag 时从同一个 tag 构建、发 Release，再重建 Pages 上的 SDK 索引，不跑测试（U-42、U-62）。

## 发布与运行版本

1. 选定 `dev` 的固定候选提交，完成该版本需要的验证；之后若候选内容变化，补验受影响的部分。
2. 把代码版本从 `.dev0` 改成正式版本号，将已验收候选集成到 `master`（`master` 是 `dev` 的祖先时快进合入），为最终发布提交创建不再移动的版本 tag（例如 `v0.4.0`），推送到 Gitea。CI 对 tag 再完整跑一次，核对 tag 在 `master` 上且等于代码版本，然后发布到 Gitea Releases，并把 SDK 以同一版本发到 Gitea 的 Python 包仓库（见下文“SDK 包仓库”）。tag、包版本、前端构建产物与依赖锁对应同一发布。
3. Gitea 上发布成功后，再把 `master` 与这个 tag 推到 GitHub；GitHub 只从 tag 构建并发布 Release，再更新 GitHub Pages 上的 SDK 包索引，不跑测试（U-42、U-62，见下文“GitHub 上的 SDK 包索引”）。随后在 `dev` 上把版本改成下一个 `.dev0`。
4. 发布包内的 `RELEASE.json` 记录发布提交、构建标识与数据库结构版本；CI 生成的发布说明只列出自上一个 tag 以来的提交；检查范围、未覆盖的限制与迁移要求由发布者在发布后到 Gitea 与 GitHub 的 Release 页补写。正式服务从独立发布目录启动，固定到 commit/tag；不通过部署目录的日常编辑或自动拉取来跟随 `dev`。`dev` 的预发布只用于试用。
5. 服务state和外置主密钥独立于发布物。上线前核活动任务、资源与所有写者，按当时版本的维护说明执行合适的升级和恢复步骤（运维见设计 [12](../design/12-operations.md)）。

数据库升级后的回退需要单独说明数据兼容或恢复路径；代码回退不代表state可以回退。旧备份不能覆盖后来新增的业务数据。

### SDK 包仓库

SDK（`maestro-sdk`）随每次发布推送到仓库所有者在 Gitea 上的 Python 包仓库：上传地址 `<Gitea 地址>/api/packages/<所有者>/pypi`，安装用的索引 `<Gitea 地址>/api/packages/<所有者>/pypi/simple`。使用者按版本安装，不需要本仓库的检出（安装命令见 [SDK 说明](../../sdk/README.md)）。

- **版本**：正式发布与代码版本相同（如 `0.4.0`）。`dev` 的每次预发布是 `<版本>.dev<CI 运行号>`，例如代码版本 `0.4.1.dev0`、运行号 1234 时为 `0.4.1.dev1234`：运行号只增不减，按 PEP 440 排在下一个正式版本之前；重跑同一次运行沿用原来的运行号。版本只写进 CI 构建出的 wheel，不提交；同一次预发布在 Releases 里附带的也是这个 wheel。包仓库不接受本地版本（`+…`），所以不用提交哈希作版本。
- **保留**：`.devN` 版本只保留最近 10 个，由发包脚本在上传后删除更早的；正式版本从不删除。已发布的文件不会被替换：重跑时文件已在且 SHA-256 相同就跳过，不同就失败（wheel 是可复现构建，同一提交得到同样的文件）。
- **推送令牌**：仓库 secret `PACKAGE_TOKEN`，只给 `write:package` 权限。所有者是组织时，令牌属于一个专用账号，该账号所在的团队对“软件包”有写权限；所有者是个人时只能用该个人的令牌。不用 job 自带的令牌，因为它能否写所有者的包取决于 Gitea 版本与配置。没有设置这个 secret 时，`dev` 的推送只给出警告、不发包，`v*` tag 的发布失败；补上 secret 后重跑即可，Release 与包都按上面的规则续上。
- **读取**：所有者在 Gitea 上公开时，任何人可以匿名安装；不公开时，使用者用只有 `read:package` 权限的令牌经 `~/.netrc` 读取（见 [SDK 说明](../../sdk/README.md#安装与连接)）。

### GitHub 上的 SDK 包索引

GitHub Packages 不支持 Python 包，PyPI 上的 `maestro-sdk` 已被别人占用，所以 GitHub 上的 SDK 由 GitHub Pages 上的静态索引提供（U-62）：`.github/workflows/release.yml` 发完 Release 后，下载所有正式 Release 附带的 SDK wheel，用 `tools/package_index.py` 写出 PEP 503 索引（链接指向 Release 里的文件并带 SHA-256），部署到 Pages。安装用的索引是 `https://<所有者>.github.io/<仓库>/simple`，与 Gitea 包仓库的用法相同；这里只有正式版本，`dev` 的预发布只在 Gitea 上。

- **第一次发布前**在 GitHub 仓库的 Settings → Pages 把 Source 选为 “GitHub Actions”；没选时 Release 照常发布，`index` 这个 job 失败，选好后重跑这个 job 即可。私有仓库的 Pages 需要付费套餐。
- 不需要令牌：job 用自带的 `GITHUB_TOKEN` 读 Release，用 Pages 的 OIDC 部署。
- 索引每次从全部正式 Release 重建，所以旧版本一直可以安装；删除一个 Release 后，下一次发布的索引里就没有它了。第一个进索引的是 0.4.0；没有附带 SDK wheel 的 Release 跳过。
