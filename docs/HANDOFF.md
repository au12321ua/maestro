# 开发交接

本页是 Maestro 的维护入口：现在的状态、代码在哪里、怎样验证。系统怎样工作见[系统说明](design/README.md)，下一步做什么见[提案索引](proposals/README.md)，决定见[决策记录](design/14-decisions.md)，分支与发布规则见[开发、协作与发布](guides/development-workflow.md)。文档的层次与提案的流程见根目录 `AGENTS.md` 与[提案索引](proposals/README.md)的“生命周期”。

## 现状

- 2026-10-24 的重写（M1～M8，从 `9ff43b5` 起）替换了旧实现；各里程碑的取舍与当时的验证范围见冻结的[实施记录](design/records/README.md)。功能见根目录 README，未实现与计划中的事见[提案索引](proposals/README.md)。
- 版本：`dev` 上是 0.4.0.dev0，准备最后验收。`master` 仍是重写前的 0.2.0；0.3.0（`1fdef62`）只在 `dev` 上、没有打 tag。0.4.0 是第一个经 `master` + `v*` tag 正式发布的版本，也是第一个出现在 GitHub Release 与 GitHub Pages 包索引里的版本。
- 部署现场：一台主机以系统级安装（`maestroctl`，默认端口 8420）运行一个实例，用户自己的端到端测试也在它上面做；主机细节和正在运行的版本由部署者记在私有仓库 `maestro-site`。开发与自动化测试不使用它的 state，用全新的独立实例（见根目录 README 的“开发试用”）。
- 远端分支 `fix/separate-verifier` 只作参考，不合入（U-32）；独立验证器已由 N-01 另行实现。

## 代码状态

- 服务在 `src/maestro/`，Runner 在 `runner/`（单独的 uv 项目和锁文件，锁定 Harbor 0.24.0），Python SDK 在 `sdk/`（开发环境以可编辑方式安装），给 Agent 的 skill 在 `skills/`（打进 SDK wheel），安装管理命令在 `ops/maestroctl.py`，测试在 `tests/maestro/`；分工见[源码职责](architecture/source-layout.md)。
- 不迁移旧 state，新实现从空实例初始化。第一个面向外部用户的版本之前不承诺向后兼容（U-31）。

## 验证入口

测试分层与验收场景见[测试与验收](design/13-testing-plan.md)；命令和门禁以本节为准。

### 前提与隔离

- 需要 Linux、Python 3.12–3.13 和 uv；真实执行另需 Docker/Compose、可用的作者依赖与 Hub 权限。依赖由 `pyproject.toml`、`uv.lock`、`frontend/package-lock.json` 记录。
- 验证使用独立 HOME、state 和主密钥，隔离 Harbor/profile，并清除继承的真实凭据环境。测试含本地子进程，不能默认没有副作用。真实 Hub、Docker、模型请求与 fixture 验证分开记录；真实模型请求须核对当前授权与预算。

### 每次提交（CI 同样检查）

- **Python**：`uv sync --locked --group dev`；`uv run ruff check src tests runner/src tools sdk/src ops` 与 `uv run ruff format --check src/maestro tests/maestro runner/src tools sdk/src ops`（CI 两者都强制）；`uv run pytest -q`（CI 用 `-n 4`），不含 Docker、Hub 与模型。
- **契约**：改了 API（路由、公开模型）后运行 `uv run python tools/generate_clients.py` 重写 `openapi/maestro-v1.json` 与 SDK 模型，再在 `frontend/` 执行 `npm run gen:api` 重写网页类型，三者都要提交。`tests/maestro/test_sdk.py` 核对前两者，`npm run check:api` 核对网页类型。新增给 PAT 用的接口时，同时给 SDK 加方法并登记到 `test_sdk.py` 的 `SDK_METHODS`。
- **数据库**：`schema.py` 与最新迁移须一致（`tests/maestro/test_migrations.py`）。0.3.0 已部署，数据库变更一律追加新迁移（U-34）；并行分支各自加了迁移时，合并后调整修订链使之保持线性，并在已有数据的实例上验证升级。
- **网页**：在 `frontend/` 执行 `npm ci`、`npm run build`、`npm run lint`、`npm run check:i18n`。build 写入 `src/maestro/static/`，随源码提交（CI 核对两者一致）。界面文字写在 `src/locales/` 的中英文语言文件里，组件里不写死文字（约定见 `src/locales/README.md`）。
- **许可证**：`uv run python tools/third_party_licenses.py`，需要服务与 Runner 的 `uv sync` 和 `frontend/` 的 `npm ci`；只检查许可证都在允许列表内。
- **文档**：`python3 tools/check_doc_links.py` 与 `git diff --check`。只改 Markdown 的推送不跑 CI，提交前在本地运行。

### 需显式开启的真实测试

这些测试不在 CI 里，按开关在本机手动运行。Runner 环境先在 `runner/` 执行 `uv sync --locked`。

- **真实 Docker（Oracle）**：`MAESTRO_DOCKER_TESTS=1 uv run pytest -q tests/maestro/test_harbor_docker.py`。只用本地 Oracle 任务和已有的 `ubuntu:22.04`，需要 buildx 与 Compose 2.24 以上，不调用模型。独立验证器与辅助服务的用例（`fixtures/n01-tasks`）拉取 `ubuntu:22.04` 时会问 Docker Hub，守护进程要能联网或有代理。每个测试模块启动一个临时镜像仓库容器（`registry:3.0.0`，`conftest.py` 的 `local_registry`，结束时删除）；执行资源都带每个测试独立的实例标签，准备好的镜像在 `<仓库>/maestro-prep/<实例>` 名下，结束时都核对清空。两项故意失败的用例核对错误分类：一个 `RUN` 步骤失败的 Dockerfile，和一个指定了不存在镜像的任务（连不上注册表时跳过）。运行前后建议对 `docker images`、`ps -a`、`network ls`、`volume ls` 各留快照对比。
- **Terminus-2**（同一文件）：需要本地镜像 `maestro-test/ubuntu-tmux:22.04`（`FROM ubuntu:22.04` 加 `apt-get install tmux`，没有时跳过）；模型是测试进程里的假服务，不花钱。它要求容器能访问 example.com，用来对照 Oracle 运行能出网、模型运行出不了网。
- **真实模型**（会产生费用）：
  - Terminus-2：`MAESTRO_DOCKER_TESTS=1 MAESTRO_MODEL_TESTS=1 MAESTRO_MODEL_ENDPOINT=<OpenAI 兼容地址> MAESTRO_MODEL_ID=<模型> MAESTRO_MODEL_KEY_FILE=<key 文件> uv run pytest -q -s tests/maestro/test_harbor_docker.py -k real_model`，一个试次、最多 5 轮。
  - OpenCode：先构建 `maestro-test/ubuntu-opencode:22.04`（`tests/maestro/fixtures/images/opencode/Dockerfile`，可经代理构建），用同样的 `MAESTRO_MODEL_*` 运行 `test_opencode_with_a_real_model`。
  - 安装路径（Codex、Claude Code 与两条 OpenCode 版本线）：`test_installed_harness_with_a_real_model`，在 setup 中真实安装。OpenCode 用 `MAESTRO_MODEL_*`；Codex 设 `MAESTRO_CODEX_ENDPOINT` / `_MODEL_ID`（Responses 地址），Claude Code 设 `MAESTRO_CLAUDE_ENDPOINT` / `_MODEL_ID`（Messages 地址）；key 文件用 `_KEY_FILE` 或默认的 `MAESTRO_MODEL_KEY_FILE`，`_VERSION` 默认是代码里记的测过的版本。另设 `MAESTRO_TEST_SETUP_PROXY`，可用 `MAESTRO_TEST_SETUP_ENV='{"npm_config_registry": …}'` 加包镜像源。
- **真实 Hub**：`MAESTRO_HUB_TESTS=1 uv run pytest -q tests/maestro/test_hub_live.py`，匿名读取公开的 `harbor/hello-world`，导入测试会真实构建（基础镜像可能被拉取）。
- **安装**：`MAESTRO_INSTALL_TESTS=1 uv run pytest -q tests/maestro/test_install.py`，覆盖安装、更新、失败回退、备份恢复、卸载与清除。它用隐藏参数 `--test-root` 把整个布局放进临时目录，以当前用户运行并注册一个 systemd **用户**单元（结束时删除），不需要 root；需要 uv、systemd 用户实例和可连接的 Docker。真正的系统级安装只能由有 sudo 的人执行，步骤见[部署指南](guides/deployment.md)。

### 环境陷阱

- **rootless Docker**：docker context 存在用户 HOME 里，隔离 HOME 会让客户端回到 `/var/run/docker.sock`，须显式设 `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`，否则 Runner 也连到系统 Docker 的套接字而失败。rootless 守护进程有自己的镜像库和代理设置，与系统守护进程不共享。
- **代理**：BuildKit 向镜像仓库取令牌由 docker 客户端进程完成，用 `env -i` 跑真实测试时须把宿主机代理（如 `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890`）放进去，否则镜像库里还没有的基础镜像（如 Harbor 出口控制 sidecar 的 `gogost/gost`）会在解析时超时。Runner 继承代理变量，但不支持 SOCKS 代理变量（`ALL_PROXY`）。容器里用的代理必须是容器能访问的地址：测试设 `MAESTRO_TEST_SETUP_PROXY=http://<容器可达的地址>:<端口>`，部署上由管理员在管理页“安装网络”设置（[12](design/12-operations.md) 第 3 节）。

### 网页冒烟

Playwright 测试需要一个全新的一次性 state：`maestro --config <tmp>/config.toml init` 记下初始化令牌，`maestro --config <tmp>/config.toml serve --port 8431 --demo-executor`，再在 `frontend/` 执行 `MAESTRO_SETUP_TOKEN=<令牌> npm run test:ui`（说明见 `qa/smoke.spec.ts` 开头）。同一个 state 只能跑一次，因为第一步是初始化管理员。

### 本机用 Harbor 真实执行

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

### 实例、打包与 CI

- **实例**：`maestro --config <config.toml> <子命令>`，子命令见 `maestro --help`（`init`、`db upgrade`、`serve`、`backup`、`restore`、`activity`、`openapi`）；示例配置与试用步骤见根目录 README。
- **打包**：`uv run python tools/build_release.py`，参数与包的内容见脚本开头的说明；`ops/maestroctl.py` 的规则由 `tests/maestro/test_maestroctl.py` 覆盖。`tools/publish_release.py`（Gitea Releases）、`tools/publish_package.py`（Gitea 包仓库）与 `tools/package_index.py`（GitHub Pages 上的 SDK 索引）只用标准库，由 `tests/maestro/test_release_tools.py` 对着模拟接口测试。版本号规则与发布步骤见[开发流程](guides/development-workflow.md)的“发布与运行版本”。
- **Gitea CI**（`.gitea/workflows/verify.yml`，在 `HH_RUNNER_LABEL` 指定的 runner 上）：每次推送运行上面“每次提交”的全部检查。只改 Markdown 的推送不跑（`skills/` 与 `sdk/README.md` 有测试核对，仍会触发；U-37、U-43），tag 总会跑。测试只传 PATH、临时 HOME 与 uv 的 Python 设置，不继承凭据或 `MAESTRO_*` 开关。下载依赖的步骤有单步时限、短超时和整步重试；仓库变量 `CI_PROXY` 非空时，只有下载依赖与打包的步骤走这个代理。
- **发布**：推送到 `dev` 且通过后，同一个 job 的最后两步把发布包发成 Gitea 预发布（tag `dev-<提交前 12 位>`，保留最近 10 个），把 SDK 发到 Gitea 的 Python 包仓库（`<版本>.dev<CI 运行号>`，保留最近 10 个）；`master` 上的 `v*` tag 再完整跑一遍，发成正式版本。写令牌只交给这两步（发 Release 用 job 自带的令牌，发包用仓库 secret `PACKAGE_TOKEN`），它们只运行标准库写的上传脚本。GitHub 上的 `.github/workflows/release.yml` 只在推送 tag 时从同一个 tag 构建、发 Release，再重建 Pages 上的 SDK 索引，不跑测试（U-42、U-62）。
- CI 的输出看 Actions 页的运行日志，不要求存成 artifacts（U-57）。

现场报告与原始证据放私有仓库 `auua/maestro-site`（规则见[开发流程](guides/development-workflow.md)的“资料与交付证据”）；公开文档只留短的固定代码版本、实际覆盖与未覆盖范围摘要。集成后的 worktree 和短期分支按协作规则收尾。
