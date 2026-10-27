# 开发交接

本页是 Maestro 的维护入口：现在的状态、代码在哪里、怎样验证。系统怎样工作见[系统说明](design/README.md)，下一步做什么见[提案索引](proposals/README.md)，决定见[决策记录](design/14-decisions.md)，分支与发布规则见[开发、协作与发布](guides/development-workflow.md)。

## 文档怎样维护

- 系统说明（`design/00`～`13`、`15`）只写现状，直接改写；待实现的设计写成[提案](proposals/README.md)，用户确认后实现，合入时并入系统说明并删除提案（U-33）。
- 用户决定与冲突写入决策记录第 0 节；尚未定论的冲突，确认之前不按任何一方擅自实现。
- 实施证据写在提交说明与 PR 描述中；需求审计稿与 `design/records/` 已冻结。

## 现状

- 2026-10-24 重写（M1～M8，从 `9ff43b5` 起）替换了旧实现，`1fdef62` 定为 0.3.0。各里程碑的取舍与当时的验证范围见冻结的[实施记录](design/records/README.md)。
- 已有：Hub 数据集导入、首次准备与共享恢复；分层配置、模型配置与 Harness（Terminus-2、OpenCode）、附件；提交、批次、重跑与实验方案；运行级执行（D-020）、评分、产物、导出与回收站；Python SDK 与网页；`maestroctl` 系统级安装、更新、同机备份恢复。未实现与计划中的事见[提案索引](proposals/README.md)。
- 部署现场：一台主机以系统级安装（`maestroctl`，默认端口 8420）运行一个实例，用户自己的端到端测试也在它上面做；主机细节由部署者维护，不写进仓库。开发与自动化测试不使用它的 state，用全新的独立实例（见根目录 README 的“开发试用”）。远端分支 `fix/separate-verifier` 未合入且不会直接合入（U-32），现场曾运行过它的构建；更新现场前核对正在运行的版本。

## 代码状态

- 服务在 `src/maestro/`，Runner 在 `runner/`（单独的 uv 项目和锁文件，锁定 Harbor 0.24.0），Python SDK 在 `sdk/`（开发环境以可编辑方式安装），安装管理命令在 `ops/maestroctl.py`，测试在 `tests/maestro/`；分工见[源码职责](architecture/source-layout.md)。
- 不迁移旧 state，新实现从空实例初始化。第一个面向外部用户的版本之前不承诺向后兼容（U-31）。

## 验证入口

需要 Linux、Python 3.12–3.13 和 uv；实际执行另需 Docker/Compose、可用作者依赖与 Hub 权限。依赖由 `pyproject.toml`、`uv.lock`、`frontend/package-lock.json` 记录。

- Python：`uv sync --locked --group dev`、`uv run pytest -q`、`uv run ruff check src tests runner/src tools sdk/src ops`；新代码另跑 `uv run ruff format --check src/maestro tests/maestro runner/src tools sdk/src ops`。
- 契约：改了 API（路由、公开模型）后运行 `uv run python tools/generate_clients.py` 重写 `openapi/maestro-v1.json` 与 SDK 模型，再在 `frontend/` 执行 `npm run gen:api` 重写网页类型；三者都要提交。`tests/maestro/test_sdk.py` 会核对前两者，`npm run check:api` 核对网页类型。新增给 PAT 用的接口时，同时给 SDK 加方法并登记到 `test_sdk.py` 的 `SDK_METHODS`。
- Runner：在 `runner/` 执行 `uv sync --locked`。真实 Docker 测试需显式开启：`MAESTRO_DOCKER_TESTS=1 uv run pytest -q tests/maestro/test_harbor_docker.py`。它只用本地 Oracle 任务和已有的 `ubuntu:22.04`，需要 buildx，不调用模型和 Hub；执行资源都带每个测试独立的实例标签，准备好的镜像在每个实例自己的 `maestro-prep/<实例>` 仓库名下，结束时都核对清空。Terminus-2 的用例需要本地镜像 `maestro-test/ubuntu-tmux:22.04`（`FROM ubuntu:22.04` 加 `apt-get install tmux`，没有时跳过），模型是测试进程里的假服务，不花钱；它还要求容器能访问 example.com，用来对照 Oracle 运行能出网、模型运行出不了网。另有两项故意失败的用例核对错误分类：一个 `RUN` 步骤失败的 Dockerfile，和一个指定了不存在镜像的任务（后者会请求注册表，守护进程连不上时跳过）。运行前后建议对 `docker images`、`ps -a`、`network ls`、`volume ls` 各留快照对比。Docker 为 rootless 时，docker context 存在用户 HOME 里，隔离 HOME 会让客户端回到 `/var/run/docker.sock`，须显式设 `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`；rootless 守护进程有自己的镜像库和代理设置，与原来的系统守护进程不共享。BuildKit 向镜像仓库取令牌由 docker 客户端进程完成，用 `env -i` 跑真实测试时须把宿主机代理（如 `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890`）放进去，否则镜像库里还没有的基础镜像（如 Harbor 出口控制 sidecar 的 `gogost/gost`）会在解析时超时。真实模型冒烟默认跳过，会产生费用，需显式开启：`MAESTRO_DOCKER_TESTS=1 MAESTRO_MODEL_TESTS=1 MAESTRO_MODEL_ENDPOINT=<OpenAI 兼容地址> MAESTRO_MODEL_ID=<模型> MAESTRO_MODEL_KEY_FILE=<key 文件> uv run pytest -q -s tests/maestro/test_harbor_docker.py -k real_model`，一个试次、最多 5 轮。真实 Hub 测试另需 `MAESTRO_HUB_TESTS=1 uv run pytest -q tests/maestro/test_hub_live.py`：匿名读取公开的 `harbor/hello-world`，导入测试会真实构建（基础镜像可能被拉取）；Runner 继承代理变量，但不支持 SOCKS 代理变量（`ALL_PROXY`）。
- 数据库：`schema.py` 与最新迁移须一致（`tests/maestro/test_migrations.py` 检查）。0.3.0 已部署，从此数据库变更一律追加新迁移，不再重新生成 `0001`（U-34）；并行分支各自加了迁移时，合并后调整修订链使之保持线性，并在已有数据的实例上验证升级。
- 实例：`maestro --config <config.toml> init / db upgrade / serve / backup / restore / openapi`；示例配置与试用步骤见根目录 README。
- 安装与发布：`uv run python tools/build_release.py` 打包（要求工作树干净，测试可加 `--allow-dirty --label <名>`）；`ops/maestroctl.py` 的规则由 `tests/maestro/test_maestroctl.py` 覆盖。完整的安装、更新、失败回退、备份恢复、卸载与清除需显式开启：`MAESTRO_INSTALL_TESTS=1 uv run pytest -q tests/maestro/test_install.py`。它用隐藏参数 `--test-root` 把整个布局放进临时目录，以当前用户运行并注册一个 systemd **用户**单元（结束时删除），不需要 root；需要 uv、systemd 用户实例和可连接的 Docker（rootless 时设 `DOCKER_HOST`）。真正的系统级安装只能由有 sudo 的人执行，步骤见[部署指南](guides/deployment.md)。
- 前端：在 `frontend/` 执行 `npm ci`、`npm run build`、`npm run lint`；build 写入 `src/maestro/static/`。OpenCode 的真实测试需要先构建 `maestro-test/ubuntu-opencode:22.04`（`tests/maestro/fixtures/images/opencode/Dockerfile`，可经代理构建），用与 Terminus 真实冒烟相同的 `MAESTRO_MODEL_*` 变量运行 `test_opencode_with_a_real_model`。主机需要代理才能顺畅准备环境、安装附件时，部署配置设 `setup_proxy`（见 [12](design/12-operations.md) 第 3 节）；真实 Docker 测试同样可设 `MAESTRO_TEST_SETUP_PROXY=http://<容器可达的地址>:<端口>`。网页冒烟测试（Playwright）需要一个全新的一次性 state：`maestro --config <tmp>/config.toml init` 记下初始化令牌，`maestro --config <tmp>/config.toml serve --port 8431 --demo-executor`，再在 `frontend/` 执行 `MAESTRO_SETUP_TOKEN=<令牌> npm run test:ui`（说明见 `qa/smoke.spec.ts` 开头）。同一个 state 只能跑一次，因为第一步是初始化管理员。
- 验证使用独立 HOME、state 和主密钥，隔离 Harbor/profile，并清除继承的真实凭据环境。测试含本地子进程，不能默认没有副作用。真实 Hub、Docker、模型请求与 fixture 验证分开记录；真实模型请求须核对当前授权与预算。
- CI：`.gitea/workflows/verify.yml` 在 `HH_RUNNER_LABEL` 指定的 runner 上对每次推送运行：ruff、`tools/generate_clients.py --check`、`tools/check_doc_links.py`（Markdown 相对链接）、不含 Docker / Hub / 模型的 pytest（只传 PATH 与临时 HOME）、网页 lint、`check:api`、build，并核对 `src/maestro/static/` 与源码一致。真实 Docker、Hub 与模型测试仍按上面的开关在本机手动运行。
- 验收场景见[测试与验收](design/13-testing-plan.md)。验证命令和门禁以本页为准。

现场报告与原始证据只保存到主仓 ignored `.local/`；公开文档只留短的固定代码版本、实际覆盖与未覆盖范围摘要。集成后的 worktree 和短期分支按协作规则收尾。
