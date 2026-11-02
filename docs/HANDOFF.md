# 开发交接

本页提供接手当前代码所需的状态、入口和本地试用步骤。先看[文档导航](README.md)，系统结构见[架构](architecture/README.md)，行为与不变量见[系统说明](design/README.md)，后续工作见[提案](proposals/README.md)。

## 当前代码

- 服务与 SDK 的声明版本均为 `0.4.0`，以各自的 `pyproject.toml` 为准；发布记录看 Git tag 与 Releases，实际运行版本由部署者记录在私有仓库 `auua/maestro-site`。
- 服务在 `src/maestro/`，独立 Runner 在 `runner/`，SDK 在 `sdk/`，随 SDK 发布的 Agent skill 在 `skills/`，安装工具在 `ops/maestroctl.py`；完整分工见[源码职责](architecture/source-layout.md)。
- 不迁移旧系统的 state；第一个面向外部用户的版本之前不承诺向后兼容（U-31）。数据库结构变更从 0.3.0 起追加迁移（U-34）。
- `fix/separate-verifier` 只作参考（U-32）；独立验证器与 Compose 辅助服务支持已经重新实现。

## 接手与验证

1. 核对根目录 `AGENTS.md`、当前任务和[用户决定](design/14-decisions.md)；按[协作流程](guides/development-workflow.md)建立分支，改代码使用独立 worktree。
2. 在任务目录执行 `uv sync --locked --group dev`；修改网页时在 `frontend/` 执行 `npm ci`。
3. 按影响范围选择[开发验证](guides/testing.md)中的命令。文档修改运行 `python3 tools/check_doc_links.py` 和 `git diff --check`；接口说明变动也可能影响生成的 OpenAPI 与客户端，须同步生成。
4. 提交时说明固定代码版本、实际覆盖和未覆盖范围；发布按[打包与发布](guides/releasing.md)，部署按[部署维护](guides/deployment.md)。

验证使用全新独立的 HOME、state 和主密钥，并清除继承的真实凭据。真实 Docker、Hub、安装与模型测试各有显式开关；真实模型请求须核对当前任务授权与预算。完整前提和开关见开发验证指南。

## 环境陷阱

- 隔离 HOME 会丢失 rootless Docker context，需显式传入正确的 `DOCKER_HOST`。
- 下载依赖的宿主机代理与容器可达的准备代理是两个配置；隔离环境时需要显式传入。具体说明见[开发验证](guides/testing.md)。

## 本机用 Harbor 真实执行

在根目录 README“开发试用”的实例上接入真实执行（Oracle 测试模式：执行任务自带的参考解法，不调用模型）。需要 Docker 和本机已有的 `ubuntu:22.04`；Harbor 装在单独的 Runner 环境里，服务本身不加载 Harbor：

```bash
(cd runner && uv sync --locked)                       # 锁定 Harbor 0.24.0
echo "runner_python = \"$PWD/runner/.venv/bin/python\"" >> "$root/config.toml"
# 准备好的镜像存在本机镜像仓库里（真实主机上由 maestroctl 运行）
docker run -d --name maestro-dev-registry -p 127.0.0.1:8425:5000 \
  -v "$root/data/registry:/var/lib/registry" -e REGISTRY_STORAGE_DELETE_ENABLED=true registry:3.0.0
uv run maestro --config "$root/config.toml" dev-local-dataset oracle tests/maestro/fixtures/harbor-tasks
uv run maestro --config "$root/config.toml" serve --port 8420
```

- `dev-local-dataset` 是开发用的隐藏命令：在服务停止时把本地任务目录当作数据源，走一遍与 Hub 相同的导入和首次准备（真实 Docker 构建，推入上面的仓库容器）。正式使用时由管理员在“数据集”页从 Hub 添加版本；私有数据集需要管理页的 `hub_token` 凭据。模型运行需要管理员先添加模型凭据、模型配置与 Harness 版本。
- `cookie_secure = false` 只用于本机 HTTP；对外部署由反向代理提供 HTTPS，保持默认的 `true`。
- 数据目录所在磁盘可用空间低于 20 GiB 或 15% 时不再受理新运行；小磁盘上调低 `config.toml` 的四个 `storage_*` 水位（键与默认值见 `src/maestro/config.py`）。
