# Maestro

[English](README.en.md)

> harbor-helper 已整体重写为 Maestro，替换了原来的实现；旧实例的数据不迁移。

Maestro 让受邀的可信团队通过网页和 SDK 提交、观察、比较和重跑基于 Harbor 的实验：数据集可选择，实验配置固定，结果全团队共享，提交身份保留。

## 功能

- Hub 数据集导入、环境的首次准备（含独立验证器与 Compose 辅助服务）与本机镜像仓库，准备好的镜像在任务间共用。
- 分层配置、模型配置与团队共用的加密凭据；Harness：Terminus-2、OpenCode 1.x 与 2.x、Codex、Claude Code。
- 按内容寻址、增量发布的附件；提交、批次、重跑与实验方案；实时执行页；区分有效零分、缺评分与执行失败的评分；按层级打包下载的产物、导出与回收站。
- 跨运行比较、集合与冻结的分析快照。
- Python SDK，内含给 Agent 的 skill 与工作区模板；中英文网页。
- `maestroctl` 系统级安装、更新、同机备份恢复与卸载。

`dev` 上是即将验收的 0.4.0，它将是第一个从 `master` 打 tag 的正式版本；此前只有 `dev` 的预发布。系统怎样工作见[系统说明](docs/design/README.md)，下一步计划见[提案索引](docs/proposals/README.md)。

## 部署

在一台带 systemd 和 Docker 的 Linux 主机上，用发布包安装。发布包从 Releases 下载：正式版本来自 `master` 的版本 tag，`dev` 每次通过 CI 后另有带提交标识的预发布，只用于试用。也可以在仓库里自己打包（见部署指南）：

```bash
sha256sum -c SHA256SUMS --ignore-missing            # 核对下载的包
tar xf maestro-<版本>-<提交>.tar.gz && cd maestro-<版本>-<提交>
sudo ./maestroctl install --uv "$(command -v uv)"   # 打印访问地址与一次性初始化令牌
```

之后用 `sudo maestroctl status / logs / backup / update <新包> / uninstall` 维护。目录布局、代理、HTTPS、更新与恢复的细节见[部署指南](docs/guides/deployment.md)。

## 让 Agent 使用

Agent 不登录服务器，只需要三样东西：[Python SDK](sdk/README.md)（skill 打包在里面）、一个个人访问令牌（PAT）和一个工作区。SDK 从包仓库按版本安装，版本与服务相同（安装细节见 [SDK 说明](sdk/README.md#安装与连接)）：

| 包仓库 | 索引地址（下文的 `<索引>`） | 内容 |
| --- | --- | --- |
| 本项目在 GitHub Pages 上的索引 | `https://<GitHub 所有者>.github.io/<仓库>/simple` | 正式版本，自 0.4.0 起 |
| 自己的 Gitea 包仓库 | `https://<Gitea 地址>/api/packages/<所有者>/pypi/simple` | 你上传的版本，可以不公开；本项目的 Gitea CI 还会推 `dev` 预发布 |

### 起一个 Gitea 包仓库

已有 Gitea 就跳过第 1 步，它的包仓库默认开启。

1. 起一个 Gitea：`docker run -d --name gitea -p 3000:3000 -v gitea-data:/data gitea/gitea:28.2.0`，打开 `http://<主机>:3000` 按默认值（SQLite）完成安装，第一个注册的账号是管理员。
2. 在“设置 → 应用”生成一个只有 `package` 读写权限的令牌，存成文件（`chmod 600`）。
3. 从 Releases 下载与服务同版本的 `maestro_sdk-<版本>-py3-none-any.whl`，上传：

   ```bash
   UV_PUBLISH_PASSWORD="$(cat <令牌文件>)" uv publish --username <用户名> \
     --publish-url http://<主机>:3000/api/packages/<用户名>/pypi maestro_sdk-<版本>-py3-none-any.whl
   ```

   包的可见性跟随所有者：所有者不公开时，Agent 所在机器要在 `~/.netrc` 写一个只读令牌（见 [SDK 说明](sdk/README.md#安装与连接)）。把本仓库托管在 Gitea 上跑 CI 时不必手动上传：设好仓库 secret `PACKAGE_TOKEN`，每次发布自动推送。

### 给 Agent 一个工作区

```bash
mkdir my-work && cd my-work
uv venv && uv pip install "maestro-sdk==<版本>" --index maestro=<索引>
.venv/bin/python -c "import maestro_sdk.workspace as w; w.init('.')"   # AGENTS.md、notes/、attachments/ 与 skill
```

在网页“设置”页创建 `experiment` 权限的 PAT，存进工作区以外的文件。在 `AGENTS.md` 里填好服务地址、令牌文件路径、可以花多少钱和当前目标，然后在这个目录里启动 Agent：skill 装在 `.agents/skills/` 与 `.claude/skills/`。不想搭包仓库时，也可以直接 `uv pip install ./maestro_sdk-<版本>-py3-none-any.whl`，但升级要自己换文件。

## 开发试用

不安装、直接从源码运行一个实例。需要 Linux、Python 3.12–3.13 和 uv；网页需先在 `frontend/` 执行 `npm ci && npm run build`。使用全新的独立目录，不要指向已有团队 state：

```bash
uv sync --locked
root=$(mktemp -d)
cat > "$root/config.toml" <<EOF
data_dir = "$root/data"
cache_dir = "$root/cache"
master_key_dir = "$root/secrets"
cookie_secure = false
EOF
uv run maestro --config "$root/config.toml" init      # 打印一次性初始化令牌
uv run maestro --config "$root/config.toml" serve --port 8420
```

打开 `http://127.0.0.1:8420`，用初始化令牌创建首个管理员。没有执行器时服务如实拒绝受理运行；开发演示可加隐藏参数 `--demo-executor`，它使用假执行器、假准备环境和一个假数据源（管理员在“数据集”页添加 `demo/arith`），结果不是真实实验。

### 用 Harbor 真实执行（Oracle 测试模式）

Harbor 装在单独的 Runner 环境里，服务本身不加载 Harbor。需要 Docker 和本机已有的 `ubuntu:22.04` 镜像：

```bash
(cd runner && uv sync --locked)                       # 锁定 Harbor 0.24.0
echo "runner_python = \"$PWD/runner/.venv/bin/python\"" >> "$root/config.toml"
# Prepared images are kept in a local image registry (maestroctl runs it on real hosts).
docker run -d --name maestro-dev-registry -p 127.0.0.1:8425:5000 \
  -v "$root/data/registry:/var/lib/registry" -e REGISTRY_STORAGE_DELETE_ENABLED=true registry:3.0.0
uv run maestro --config "$root/config.toml" dev-local-dataset oracle tests/maestro/fixtures/harbor-tasks
uv run maestro --config "$root/config.toml" serve --port 8420
```

`dev-local-dataset` 是开发用的隐藏命令：在服务停止时把本地任务目录当作数据源，走一遍与 Hub 相同的导入和首次准备（真实 Docker 构建，推入本机镜像仓库，即上面用 `docker run` 起的仓库容器）。正式使用时，管理员在“数据集”页从 Hub 搜索并添加版本；Hub 访问需要网络，私有数据集需要在管理页添加 `hub_token` 凭据并在添加时选择。这里用的是 Oracle 测试模式：执行任务自带的参考解法，不调用模型；模型运行需要管理员先在管理页添加模型凭据、模型配置与 Harness 版本。运行页能看到每个试次的步骤、耗时和实时日志。

`cookie_secure = false` 只用于本机 HTTP；对外部署由反向代理提供 HTTPS，并保持默认的 `true`。

与机器和磁盘有关的上限（证据与导出大小、构建与拉取时限、并发、Runner 心跳、磁盘水位）都在 `config.toml`，键、默认值与说明见 `src/maestro/config.py`，写错或拼错的键会拒绝启动并指出是哪一项；实际生效的值见 `/api/v1/capabilities` 的 `limits`。数据目录所在磁盘的可用空间低于 20 GiB 或 15% 时不再受理新运行，小磁盘上试用要调低四个 `storage_*` 水位。PAT 期限、运行规模、保留天数等团队策略由管理员在网页“管理 → 团队策略”调整。

## 许可证

[Apache License 2.0](LICENSE)，另见 [NOTICE](NOTICE)。每个发布包附第三方许可证清单 `THIRD_PARTY_LICENSES.md`。

## 文档

- [文档导航](docs/README.md)、[开发交接](docs/HANDOFF.md)、[源码职责](docs/architecture/source-layout.md)。
- 公开 API：受控副本 `openapi/maestro-v1.json`；运行中的服务在 `/api/v1/openapi.json`，离线 `uv run maestro openapi`。
