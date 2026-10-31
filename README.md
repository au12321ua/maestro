# Maestro

[English](README.en.md)

Maestro 让受邀的可信团队通过网页和 SDK 提交、观察、比较和重跑基于 Harbor 的实验：数据集可选择，实验配置固定，结果全团队共享，提交身份保留。

## 功能

- Hub 数据集导入、环境的首次准备（含独立验证器与 Compose 辅助服务）与本机镜像仓库，准备好的镜像在任务间共用。
- 分层配置、模型配置与团队共用的加密凭据；Harness：Terminus-2、OpenCode 1.x 与 2.x、Codex、Claude Code。
- 按内容寻址、增量发布的附件；提交、批次、重跑与实验方案；实时执行页；区分有效零分、缺评分与执行失败的评分；按层级打包下载的产物、导出与回收站。
- 跨运行比较、集合与冻结的分析快照。
- Python SDK，内含给 Agent 的 skill 与工作区模板；中英文网页。
- `maestroctl` 系统级安装、更新、同机备份恢复与卸载。

系统怎样工作见[系统说明](docs/design/README.md)，下一步计划见[提案索引](docs/proposals/README.md)。

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
| 本项目在 GitHub Pages 上的索引 | `https://au12321ua.github.io/maestro/simple` | 自 0.4.0 起 |

### 给 Agent 一个工作区

```bash
mkdir my-work && cd my-work
uv venv && uv pip install "maestro-sdk==<版本>" --index maestro=<索引>
.venv/bin/python -c "import maestro_sdk.workspace as w; w.init('.')"   # AGENTS.md、notes/、attachments/ 与 skill
```

在网页“设置”页创建 `experiment` 权限的 PAT，存进工作区以外的文件。在 `AGENTS.md` 里填好服务地址、令牌文件路径、可以花多少钱和当前目标，然后在这个目录里启动 Agent：skill 装在 `.agents/skills/` 与 `.claude/skills/`。不用包索引时，也可以直接 `uv pip install ./maestro_sdk-<版本>-py3-none-any.whl`，但升级要自己换文件。

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

## 许可证

[Apache License 2.0](LICENSE)，另见 [NOTICE](NOTICE)。每个发布包附第三方许可证清单 `THIRD_PARTY_LICENSES.md`。

## 文档

- [文档导航](docs/README.md)、[开发交接](docs/HANDOFF.md)、[源码职责](docs/architecture/source-layout.md)。
- 公开 API：受控副本 `openapi/maestro-v1.json`；运行中的服务在 `/api/v1/openapi.json`，离线 `uv run maestro openapi`。
