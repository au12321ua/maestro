# 开发交接

本页是 Maestro 的维护入口：现在的状态、代码在哪里、怎样验证。系统怎样工作见[系统说明](design/README.md)，下一步做什么见[提案索引](proposals/README.md)，决定见[决策记录](design/14-decisions.md)，分支与发布规则见[开发、协作与发布](guides/development-workflow.md)。

## 文档怎样维护

- 系统说明（`design/00`～`13`、`15`）只写现状，直接改写；待实现的设计写成[提案](proposals/README.md)，用户确认后实现，合入时并入系统说明并删除提案（U-33）。
- 用户决定与冲突写入决策记录第 0 节；尚未定论的冲突，确认之前不按任何一方擅自实现。
- 实施证据写在提交说明与 PR 描述中；需求审计稿与 `design/records/` 已冻结。

## 现状

- 2026-10-24 重写（M1～M8，从 `9ff43b5` 起）替换了旧实现，`1fdef62` 定为 0.3.0。各里程碑的取舍与当时的验证范围见冻结的[实施记录](design/records/README.md)。
- 已有：Hub 数据集导入、首次准备与共享恢复；分层配置、模型配置与 Harness（Terminus-2、OpenCode 1.x 与 2.x、Codex、Claude Code）、附件；提交、批次、重跑与实验方案；跨运行比较、集合与分析快照；运行级执行（D-020）、评分、产物、导出与回收站；Python SDK 与网页；`maestroctl` 系统级安装、更新、同机备份恢复。未实现与计划中的事见[提案索引](proposals/README.md)。
- 部署现场：一台主机以系统级安装（`maestroctl`，默认端口 8420）运行一个实例，用户自己的端到端测试也在它上面做；主机细节由部署者维护，不写进仓库。开发与自动化测试不使用它的 state，用全新的独立实例（见根目录 README 的“开发试用”）。远端分支 `fix/separate-verifier` 未合入且不会直接合入（U-32），现场曾运行过它的构建；更新现场前核对正在运行的版本。

## 代码状态

- 服务在 `src/maestro/`，Runner 在 `runner/`（单独的 uv 项目和锁文件，锁定 Harbor 0.24.0），Python SDK 在 `sdk/`（开发环境以可编辑方式安装），安装管理命令在 `ops/maestroctl.py`，测试在 `tests/maestro/`；分工见[源码职责](architecture/source-layout.md)。
- 不迁移旧 state，新实现从空实例初始化。第一个面向外部用户的版本之前不承诺向后兼容（U-31）。

## 验证入口

需要 Linux、Python 3.12–3.13 和 uv；实际执行另需 Docker/Compose、可用作者依赖与 Hub 权限。依赖由 `pyproject.toml`、`uv.lock`、`frontend/package-lock.json` 记录。

- Python：`uv sync --locked --group dev`、`uv run pytest -q`、`uv run ruff check src tests runner/src tools sdk/src ops`；新代码另跑 `uv run ruff format --check src/maestro tests/maestro runner/src tools sdk/src ops`。
- 契约：改了 API（路由、公开模型）后运行 `uv run python tools/generate_clients.py` 重写 `openapi/maestro-v1.json` 与 SDK 模型，再在 `frontend/` 执行 `npm run gen:api` 重写网页类型；三者都要提交。`tests/maestro/test_sdk.py` 会核对前两者，`npm run check:api` 核对网页类型。新增给 PAT 用的接口时，同时给 SDK 加方法并登记到 `test_sdk.py` 的 `SDK_METHODS`。
- Runner：在 `runner/` 执行 `uv sync --locked`。真实 Docker 测试需显式开启：`MAESTRO_DOCKER_TESTS=1 uv run pytest -q tests/maestro/test_harbor_docker.py`。它只用本地 Oracle 任务和已有的 `ubuntu:22.04`，需要 buildx 与 Compose 2.24 以上，不调用模型；独立验证器与辅助服务的用例（`fixtures/n01-tasks`）拉取 `ubuntu:22.04` 时会问 Docker Hub，守护进程要能联网或有代理。每个测试模块启动一个临时镜像仓库容器（`registry:3.0.0`，`conftest.py` 的 `local_registry`，结束时删除）；执行资源都带每个测试独立的实例标签，准备好的镜像在 `<仓库>/maestro-prep/<实例>` 名下，结束时都核对清空。Terminus-2 的用例需要本地镜像 `maestro-test/ubuntu-tmux:22.04`（`FROM ubuntu:22.04` 加 `apt-get install tmux`，没有时跳过），模型是测试进程里的假服务，不花钱；它还要求容器能访问 example.com，用来对照 Oracle 运行能出网、模型运行出不了网。另有两项故意失败的用例核对错误分类：一个 `RUN` 步骤失败的 Dockerfile，和一个指定了不存在镜像的任务（后者会请求注册表，守护进程连不上时跳过）。运行前后建议对 `docker images`、`ps -a`、`network ls`、`volume ls` 各留快照对比。Docker 为 rootless 时，docker context 存在用户 HOME 里，隔离 HOME 会让客户端回到 `/var/run/docker.sock`，须显式设 `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`；rootless 守护进程有自己的镜像库和代理设置，与原来的系统守护进程不共享。BuildKit 向镜像仓库取令牌由 docker 客户端进程完成，用 `env -i` 跑真实测试时须把宿主机代理（如 `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890`）放进去，否则镜像库里还没有的基础镜像（如 Harbor 出口控制 sidecar 的 `gogost/gost`）会在解析时超时。真实模型冒烟默认跳过，会产生费用，需显式开启：`MAESTRO_DOCKER_TESTS=1 MAESTRO_MODEL_TESTS=1 MAESTRO_MODEL_ENDPOINT=<OpenAI 兼容地址> MAESTRO_MODEL_ID=<模型> MAESTRO_MODEL_KEY_FILE=<key 文件> uv run pytest -q -s tests/maestro/test_harbor_docker.py -k real_model`，一个试次、最多 5 轮。真实 Hub 测试另需 `MAESTRO_HUB_TESTS=1 uv run pytest -q tests/maestro/test_hub_live.py`：匿名读取公开的 `harbor/hello-world`，导入测试会真实构建（基础镜像可能被拉取）；Runner 继承代理变量，但不支持 SOCKS 代理变量（`ALL_PROXY`）。
- 数据库：`schema.py` 与最新迁移须一致（`tests/maestro/test_migrations.py` 检查）。0.3.0 已部署，从此数据库变更一律追加新迁移，不再重新生成 `0001`（U-34）；并行分支各自加了迁移时，合并后调整修订链使之保持线性，并在已有数据的实例上验证升级。
- 实例：`maestro --config <config.toml> init / db upgrade / serve / backup / restore / openapi`；示例配置与试用步骤见根目录 README。
- 安装与发布：`uv run python tools/build_release.py` 打包（要求工作树干净，测试可加 `--allow-dirty --label <名>`；`--assets` 另写出 SDK wheel、许可证清单与 `SHA256SUMS`，`--expect-version` 核对代码版本，`--dev-number N` 把 SDK wheel 的版本从 `.dev0` 换成 `.devN`，只改构建出的 wheel，`sdk/pyproject.toml` 构建后即还原；SDK 与服务的版本不同时拒绝打包）。打包会运行 `tools/third_party_licenses.py`，需要服务与 Runner 的 `uv sync` 和 `frontend/` 的 `npm ci`；它单独运行时只检查许可证都在允许列表内。`tools/publish_release.py` 把产物发布到 Gitea Releases，`tools/publish_package.py` 把 SDK wheel 发布到 Gitea 的 Python 包仓库，两者与许可证规则一起由 `tests/maestro/test_release_tools.py` 对着模拟接口测试。`dev` 上的版本号是下一个版本加 `.dev0`，正式发布时改成正式版本号，发布后立即改成再下一个 `.dev0`；`ops/maestroctl.py` 的规则由 `tests/maestro/test_maestroctl.py` 覆盖。完整的安装、更新、失败回退、备份恢复、卸载与清除需显式开启：`MAESTRO_INSTALL_TESTS=1 uv run pytest -q tests/maestro/test_install.py`。它用隐藏参数 `--test-root` 把整个布局放进临时目录，以当前用户运行并注册一个 systemd **用户**单元（结束时删除），不需要 root；需要 uv、systemd 用户实例和可连接的 Docker（rootless 时设 `DOCKER_HOST`）。真正的系统级安装只能由有 sudo 的人执行，步骤见[部署指南](guides/deployment.md)。
- 前端：在 `frontend/` 执行 `npm ci`、`npm run build`、`npm run lint`、`npm run check:i18n`；build 写入 `src/maestro/static/`。界面文字写在 `src/locales/` 的中英文语言文件里，组件里不写死文字（N-18，约定见 `src/locales/README.md`）。OpenCode 的真实测试需要先构建 `maestro-test/ubuntu-opencode:22.04`（`tests/maestro/fixtures/images/opencode/Dockerfile`，可经代理构建），用与 Terminus 真实冒烟相同的 `MAESTRO_MODEL_*` 变量运行 `test_opencode_with_a_real_model`。Codex、Claude Code 与两条 OpenCode 版本线的安装路径真实测试是 `test_installed_harness_with_a_real_model`（OpenCode 用 `MAESTRO_MODEL_*`），在 setup 中真实安装：分别设 `MAESTRO_CODEX_ENDPOINT` / `_MODEL_ID`（Responses 地址）与 `MAESTRO_CLAUDE_ENDPOINT` / `_MODEL_ID`（Messages 地址），key 文件用 `_KEY_FILE` 或默认 `MAESTRO_MODEL_KEY_FILE`，`_VERSION` 默认是代码里记的测过的版本；另设 `MAESTRO_TEST_SETUP_PROXY`，可用 `MAESTRO_TEST_SETUP_ENV='{"npm_config_registry": …}'` 加包镜像源。rootless Docker 须设 `DOCKER_HOST`，否则 Runner 连到系统 Docker 的套接字而失败。主机需要代理或包镜像源才能顺畅准备环境、安装 Harness 与附件时，管理员在管理页“安装网络”设置（见 [12](design/12-operations.md) 第 3 节）；真实 Docker 测试同样可设 `MAESTRO_TEST_SETUP_PROXY=http://<容器可达的地址>:<端口>`。网页冒烟测试（Playwright）需要一个全新的一次性 state：`maestro --config <tmp>/config.toml init` 记下初始化令牌，`maestro --config <tmp>/config.toml serve --port 8431 --demo-executor`，再在 `frontend/` 执行 `MAESTRO_SETUP_TOKEN=<令牌> npm run test:ui`（说明见 `qa/smoke.spec.ts` 开头）。同一个 state 只能跑一次，因为第一步是初始化管理员。
- 验证使用独立 HOME、state 和主密钥，隔离 Harbor/profile，并清除继承的真实凭据环境。测试含本地子进程，不能默认没有副作用。真实 Hub、Docker、模型请求与 fixture 验证分开记录；真实模型请求须核对当前授权与预算。
- CI：`.gitea/workflows/verify.yml` 在 `HH_RUNNER_LABEL` 指定的 runner 上对每次推送运行：ruff、`tools/check_doc_links.py`（Markdown 相对链接）、不含 Docker / Hub / 模型的 pytest（`-n 4` 并行，只传 PATH 与临时 HOME；契约与 SDK 模型漂移由其中的 `test_sdk.py` 检查）、网页 lint、`check:api`、`check:i18n`、build、核对 `src/maestro/static/` 与源码一致，以及第三方许可证检查。下载依赖的步骤有单步时限、短超时和整步重试；仓库变量 `CI_PROXY` 非空时，只有下载依赖与打包的步骤走这个代理。推送到 `dev` 且通过后，同一个 job 的最后两步把发布包发成 Gitea 预发布（tag `dev-<提交前 12 位>`，保留最近 10 个），把 SDK 发到 Gitea 的 Python 包仓库（版本 `<版本>.dev<CI 运行号>`，保留最近 10 个）；`master` 上的 `v*` tag 再完整跑一遍并发成正式版本，tag 必须等于代码版本，SDK 以同一版本发到包仓库。写令牌只交给这两步（发 Release 用 job 自带的令牌，发包用仓库 secret `PACKAGE_TOKEN`，见[开发流程](guides/development-workflow.md)的“SDK 包仓库”），它们只运行标准库写的上传脚本；不用 Actions artifacts 在 job 之间传递，因为 runner 的 job 容器上传 artifact 时曾连接被重置；CI 证据也不要求存成 artifacts（U-57）。GitHub 上的 `.github/workflows/release.yml` 只在推送 tag 时从同一个 tag 构建并发布，不跑测试（U-42）。只改 Markdown 的推送不跑 CI（工作流的 `paths`，U-37、U-43）；`skills/` 与 `sdk/README.md` 有测试核对，仍会触发。真实 Docker、Hub 与模型测试仍按上面的开关在本机手动运行。
- 验收场景见[测试与验收](design/13-testing-plan.md)。验证命令和门禁以本页为准。

现场报告与原始证据放私有仓库 `auua/maestro-site`（主密钥文件、初始化令牌、Gitea 凭据和 SSH 私钥不入库，其余日志不要求逐项脱敏，U-36）；公开文档只留短的固定代码版本、实际覆盖与未覆盖范围摘要。集成后的 worktree 和短期分支按协作规则收尾。
