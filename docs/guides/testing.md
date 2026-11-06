# 开发验证

导航：[开发交接](../HANDOFF.md) · [测试与验收](../design/13-testing-plan.md) · [协作流程](development-workflow.md)

本页维护验证命令、隔离前提与 CI 范围；场景及预期结果在测试与验收文档中。所有命令从仓库根目录执行，标明在 `frontend/` 或 `runner/` 中运行的除外。

## 前提与隔离

- 需要 Linux、Python 3.12–3.13 和 uv；真实执行另需 Docker/Compose、可用的作者依赖与 Hub 权限。依赖由 `pyproject.toml`、`uv.lock`、`frontend/package-lock.json` 记录。
- 验证使用独立 HOME、state 和主密钥，隔离 Harbor/profile，并清除继承的真实凭据环境。测试含本地子进程，不能默认没有副作用。真实 Hub、Docker、模型请求与 fixture 验证分开记录；真实模型请求须核对当前授权与预算。

## 测试的组织

`tests/maestro/` 按模块分目录：

| 目录 | 内容 |
| --- | --- |
| `identity/` | 账户、会话、登录限流、邀请、PAT 权限、凭据与主密钥、脱敏 |
| `datasets/` | 导入、准入、准备与缓存恢复、任务材料、镜像准备、Hub 检索 |
| `configuration/` | 团队策略与模型配置、实验方案、Harness 版本、安装网络、附件 |
| `runs/` | 提交与幂等、批次、重跑、容量、调度、重启恢复、执行器与错误策略 |
| `results/` | 计分与契约、比较、集合、导出、产物下载与打包、保留、回收站 |
| `runner/` | Runner 进程内的网络收紧、附件安装、Harness 与证据收集（需 `runner/` 的 uv 环境） |
| `ops/` | 配置文件、命令行、迁移、备份、`maestroctl`、发布工具 |
| `api/` | OpenAPI 契约、SDK 客户端、静态网页 |
| `e2e/` | [13](../design/13-testing-plan.md) 的三个故事：只用 PAT 经 HTTP、只用 SDK |
| `live/` | 需显式开启的真实 Docker、Hub、模型与安装测试（见下文） |

- 新测试放进所测行为所在的文件，按行为命名；修问题的回归测试同样如此，不按 issue 或审查轮次建文件。
- 多个文件共用的辅助函数放 `support/<模块>.py`，多处共用的 fixture 放根目录的 `conftest.py`；测试文件之间不互相导入。仓库与 fixture 路径用 `support` 的 `ROOT`、`FIXTURES`。
- 各目录没有 `__init__.py`，测试文件名在整个 `tests/maestro/` 中必须唯一（重名时 pytest 报错）。

## 每次提交（CI 同样检查）

- **Python**：`uv sync --locked --group dev`；`uv run ruff check src tests runner/src tools sdk/src ops` 与 `uv run ruff format --check src/maestro tests/maestro runner/src tools sdk/src ops`（CI 两者都强制）；`uv run pytest -q`（CI 用 `-n 4`），不含 Docker、Hub 与模型。
- **契约**：改了 API（路由、公开模型）后运行 `uv run python tools/generate_clients.py` 重写 `openapi/maestro-v1.json` 与 SDK 模型，再在 `frontend/` 执行 `npm run gen:api` 重写网页类型，三者都要提交。`tests/maestro/api/test_sdk.py` 核对前两者，`npm run check:api` 核对网页类型。新增给 PAT 用的接口时，同时给 SDK 加方法并登记到 `api/test_sdk.py` 的 `SDK_METHODS`。
- **数据库**：`schema.py` 与最新迁移须一致（`tests/maestro/ops/test_migrations.py`）。0.3.0 已部署，数据库变更一律追加新迁移（U-34）；并行分支各自加了迁移时，合并后调整修订链使之保持线性，并在已有数据的实例上验证升级。
- **网页**：在 `frontend/` 执行 `npm ci`、`npm run build`、`npm run lint`、`npm run check:i18n`。build 写入 `src/maestro/static/`，随源码提交（CI 核对两者一致）。界面文字写在 `src/locales/` 的中英文语言文件里，组件里不写死文字（约定见 [语言文件约定](../../frontend/src/locales/README.md)）。
- **许可证**：`uv run python tools/third_party_licenses.py`，需要服务与 Runner 的 `uv sync` 和 `frontend/` 的 `npm ci`；只检查许可证都在允许列表内。
- **文档**：`python3 tools/check_doc_links.py` 与 `git diff --check`。只改 Markdown 的推送不跑 CI，提交前在本地运行。

## 需显式开启的真实测试

这些测试不在 CI 里，按开关在本机手动运行。Runner 环境先在 `runner/` 执行 `uv sync --locked`。

- **真实 Docker（Oracle）**：`MAESTRO_DOCKER_TESTS=1 uv run pytest -q tests/maestro/live/test_harbor_docker.py`。只用本地 Oracle 任务和已有的 `ubuntu:22.04`，需要 buildx 与 Compose 2.24 以上，不调用模型。独立验证器与辅助服务的用例（`fixtures/n01-tasks`）拉取 `ubuntu:22.04` 时会问 Docker Hub，守护进程要能联网或有代理。每个测试模块启动一个临时镜像仓库容器（`registry:3.0.0`，`conftest.py` 的 `local_registry`，结束时删除）；执行资源都带每个测试独立的实例标签，准备好的镜像在 `<仓库>/maestro-prep/<实例>` 名下，结束时都核对清空。两项故意失败的用例核对错误分类：一个 `RUN` 步骤失败的 Dockerfile，和一个指定了不存在镜像的任务（连不上注册表时跳过）。运行前后建议对 `docker images`、`ps -a`、`network ls`、`volume ls` 各留快照对比。
- **Terminus-2**（同一文件）：需要本地镜像 `maestro-test/ubuntu-tmux:22.04`（`FROM ubuntu:22.04` 加 `apt-get install tmux`，没有时跳过）；模型是测试进程里的假服务，不花钱。它要求容器能访问 example.com，用来对照 Oracle 运行能出网、模型运行出不了网。
- **真实模型**（会产生费用）：
  - Terminus-2：`MAESTRO_DOCKER_TESTS=1 MAESTRO_MODEL_TESTS=1 MAESTRO_MODEL_ENDPOINT=<OpenAI 兼容地址> MAESTRO_MODEL_ID=<模型> MAESTRO_MODEL_KEY_FILE=<key 文件> uv run pytest -q -s tests/maestro/live/test_harbor_docker.py -k real_model`，一个试次、最多 5 轮。
  - OpenCode：先构建 `maestro-test/ubuntu-opencode:22.04`（`tests/maestro/fixtures/images/opencode/Dockerfile`，可经代理构建），用同样的 `MAESTRO_MODEL_*` 运行 `test_opencode_with_a_real_model`。
  - 安装路径（Codex、Claude Code 与两条 OpenCode 版本线）：`test_installed_harness_with_a_real_model`，在 setup 中真实安装。OpenCode 用 `MAESTRO_MODEL_*`；Codex 设 `MAESTRO_CODEX_ENDPOINT` / `_MODEL_ID`（Responses 地址），Claude Code 设 `MAESTRO_CLAUDE_ENDPOINT` / `_MODEL_ID`（Messages 地址）；key 文件用 `_KEY_FILE` 或默认的 `MAESTRO_MODEL_KEY_FILE`，`_VERSION` 默认是代码里记的测过的版本。另设 `MAESTRO_TEST_SETUP_PROXY`，可用 `MAESTRO_TEST_SETUP_ENV='{"npm_config_registry": …}'` 加包镜像源。
- **真实 Hub**：`MAESTRO_HUB_TESTS=1 uv run pytest -q tests/maestro/live/test_hub_live.py`，匿名读取公开的 `harbor/hello-world`，导入测试会真实构建（基础镜像可能被拉取）。
- **安装**：`MAESTRO_INSTALL_TESTS=1 uv run pytest -q tests/maestro/live/test_install.py`，覆盖安装、更新、失败回退、备份恢复、卸载与清除。它用隐藏参数 `--test-root` 把整个布局放进临时目录，以当前用户运行并注册一个 systemd **用户**单元（结束时删除），不需要 root；需要 uv、systemd 用户实例和可连接的 Docker。真正的系统级安装只能由有 sudo 的人执行，步骤见[部署指南](deployment.md)。

## 环境陷阱

- **rootless Docker**：docker context 存在用户 HOME 里，隔离 HOME 会让客户端回到 `/var/run/docker.sock`，须显式设 `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`，否则 Runner 也连到系统 Docker 的套接字而失败。rootless 守护进程有自己的镜像库和代理设置，与系统守护进程不共享。
- **代理**：BuildKit 向镜像仓库取令牌由 docker 客户端进程完成，用 `env -i` 跑真实测试时须把宿主机代理（如 `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890`）放进去，否则镜像库里还没有的基础镜像（如 Harbor 出口控制 sidecar 的 `gogost/gost`）会在解析时超时。Runner 继承代理变量，但不支持 SOCKS 代理变量（`ALL_PROXY`）。容器里用的代理必须是容器能访问的地址：测试设 `MAESTRO_TEST_SETUP_PROXY=http://<容器可达的地址>:<端口>`，部署上由管理员在管理页“安装网络”设置（[12](../design/12-operations.md) 第 3 节）。

## 网页冒烟

Playwright 测试需要一个全新的一次性 state：`maestro --config <tmp>/config.toml init` 记下初始化令牌，`maestro --config <tmp>/config.toml serve --port 8431 --demo-executor`，再在 `frontend/` 执行 `MAESTRO_SETUP_TOKEN=<令牌> npm run test:ui`（说明见 [smoke.spec.ts](../../frontend/qa/smoke.spec.ts) 开头）。同一个 state 只能跑一次，因为第一步是初始化管理员。数据集上传用例要服务用 Runner 检查题目：先在 `runner/` 执行 `uv sync --locked`，在该 state 的 `config.toml` 里加 `runner_python = "<仓库>/runner/.venv/bin/python"`（只做本地解析，不用 Docker）；没有配置时该用例跳过。

## 打包与 CI

- **打包**：`uv run python tools/build_release.py`，参数与包的内容见脚本开头的说明；`ops/maestroctl.py` 的规则由 `tests/maestro/ops/test_maestroctl.py` 覆盖。`tools/publish_release.py`（Gitea/GitHub Releases）、`tools/publish_package.py`（Gitea 包仓库）与 `tools/package_index.py`（GitHub Pages 上的 SDK 索引）只用标准库，由 `tests/maestro/ops/test_release_tools.py` 对着模拟接口测试；其中还覆盖准确 SHA 的检查资格、草稿续传、不可覆盖、可复现归档及并发 dev 收尾。`tools/release.py` 是维护者准备与续跑入口，`tools/version.py` 同步权威版本和生成物，`tools/validate_release.py` 在全新环境安装实际 wheel、核对版本、入口与网页。发布步骤见[打包与发布](releasing.md)。
- **Gitea CI**（`.gitea/workflows/verify.yml`，在 `HH_RUNNER_LABEL` 指定的 runner 上）：每次推送运行本页“每次提交”的全部检查。只改 Markdown 的推送不跑（`skills/` 与 `sdk/README.md` 有测试核对，仍会触发；U-37、U-43），tag 总会触发，但只执行独立的发布任务：复用准确 SHA 已成功的完整检查，再构建并验证产物，不重跑源码测试。PR 不重复触发，新的合并 SHA 照常按 push 检查；手动运行 `verify` 可补验文档候选或缺失的自动运行。测试只传 PATH、临时 HOME 与 uv 的 Python 设置，不继承凭据或 `MAESTRO_*` 开关。下载依赖的步骤有单步时限、短超时和整步重试；仓库变量 `CI_PROXY` 非空时，只有下载依赖与打包的步骤走这个代理。
- 检查与发布分别显示为 `verify`、`publish`，上传失败不会改变源码检查结果。发布门禁核对预期工作流、完整 SHA、任务与必需步骤实际成功；仅看整个运行的颜色不足以放行。直接 push 的结果是事后验证，分支保护需实时核对，不能由工作流推断。
- CI 的输出看 Actions 页的运行日志，不要求存成 artifacts（U-57）。
