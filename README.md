# Maestro

> harbor-helper 已整体重写为 Maestro，替换了原来的实现；旧实例的数据不迁移。

Maestro 让受邀的可信团队通过网页和 SDK 提交、观察、比较和重跑基于 Harbor 的实验：数据集可选择，实验配置固定，结果全团队共享，提交身份保留。

## 当前状态

0.3.0 已有：Hub 数据集导入与环境准备、分层配置与 Harness（Terminus-2、OpenCode 1.x 与 2.x、Codex、Claude Code）、附件、提交与批次、重跑、实时执行页、评分与产物、导出与回收站、Python SDK，以及 `maestroctl` 系统级安装、更新、备份恢复与卸载。系统怎样工作见[系统说明](docs/design/README.md)，下一步计划见[提案索引](docs/proposals/README.md)。

## 部署

在一台带 systemd 和 Docker 的 Linux 主机上，用发布包安装。发布包从 Releases 下载：正式版本来自 `master` 的版本 tag，`dev` 每次通过 CI 后另有带提交标识的预发布，只用于试用。也可以在仓库里自己打包（见部署指南）：

```bash
sha256sum -c SHA256SUMS --ignore-missing            # 核对下载的包
tar xf maestro-<版本>.tar.gz && cd maestro-<版本>
sudo ./maestroctl install --uv "$(command -v uv)"   # 打印访问地址与一次性初始化令牌
```

之后用 `sudo maestroctl status / logs / backup / update <新包> / uninstall` 维护。目录布局、代理、HTTPS、更新与恢复的细节见[部署指南](docs/guides/deployment.md)。

Agent 或脚本用 [Python SDK](sdk/README.md)：`pip install ./sdk`，在网页“设置”页创建 PAT 后即可解析配置、提交、等待和下载结果，不需要登录服务器。

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
uv run maestro --config "$root/config.toml" dev-local-dataset oracle tests/maestro/fixtures/harbor-tasks
uv run maestro --config "$root/config.toml" serve --port 8420
```

`dev-local-dataset` 是开发用的隐藏命令：在服务停止时把本地任务目录当作数据源，走一遍与 Hub 相同的导入和首次准备（真实 Docker 构建并保存原件）。正式使用时，管理员在“数据集”页从 Hub 搜索并添加版本；Hub 访问需要网络，私有数据集需要在管理页添加 `hub_token` 凭据并在添加时选择。这里用的是 Oracle 测试模式：执行任务自带的参考解法，不调用模型；模型运行需要管理员先在管理页添加模型凭据、模型配置与 Harness 版本。运行页能看到每个试次的步骤、耗时和实时日志。

`cookie_secure = false` 只用于本机 HTTP；对外部署由反向代理提供 HTTPS，并保持默认的 `true`。

数据目录所在磁盘的可用空间低于 20 GiB 或 15% 时不再受理新运行，低于 10 GiB 或 8% 时排队运行不再启动。小磁盘上试用可在 `config.toml` 调低 `storage_accept_min_free_gb`、`storage_accept_min_free_percent`、`storage_dispatch_min_free_gb`、`storage_dispatch_min_free_percent`。

与机器和磁盘有关的其他上限也在 `config.toml`，都是整数，默认值适合一台普通开发机，启动时会检查范围，写错或拼错的键会拒绝启动并指出是哪一项：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `artifact_max_files` / `artifact_file_max_mb` / `artifact_attempt_max_mb` | 500 / 64 / 1024 | 每次执行尝试保留的证据：文件数、单个文件、总量；超出的文件在运行页标为跳过 |
| `export_max_gb` | 10 | 单个批量导出包的上限；小磁盘上应同时调低 |
| `build_timeout_default_sec` / `build_timeout_max_sec` | 600 / 3600 | 准备环境时 docker 构建的时限：任务没有声明时用默认，任务自己声明的不超过上限 |
| `pull_timeout_sec` / `save_timeout_sec` | 1800 / 1800 | 拉取镜像、导出与载入准备原件的时限 |
| `restore_slots` / `hub_runner_slots` | 2 / 4 | 同时进行的准备或恢复数；同时运行的 Hub 检索与下载进程数 |
| `runner_heartbeat_sec` / `runner_silence_sec` | 10 / 120 | Runner 的心跳间隔，以及多久没有任何动静就视为失联；归档很大的目录会阻塞心跳，在慢盘上调大归档上限时请同时调大静默时限 |

实际生效的值可在 `/api/v1/capabilities` 的 `limits` 里查到。PAT 期限、单次运行的规模、保留天数等团队策略在数据库里按修订保存，由管理员在网页“管理 → 团队策略”调整。

## 许可证

[Apache License 2.0](LICENSE)，另见 [NOTICE](NOTICE)。每个发布包附第三方许可证清单 `THIRD_PARTY_LICENSES.md`。

## 文档

- [文档导航](docs/README.md)、[开发交接](docs/HANDOFF.md)、[源码职责](docs/architecture/source-layout.md)。
- 公开 API：受控副本 `openapi/maestro-v1.json`；运行中的服务在 `/api/v1/openapi.json`，离线 `uv run maestro openapi`。
