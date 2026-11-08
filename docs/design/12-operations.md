# 12 部署运维与可观测性

导航：[总览](00-overview.md) · 依赖：[执行](05-scheduling-execution.md)、[存储](07-artifacts-export.md)、[权限](09-identity-security.md) · 相关代码：`ops/maestroctl.py`、`tools/build_release.py`、`src/maestro/backup.py`、`src/maestro/config.py`；操作步骤见[部署指南](../guides/deployment.md)

## 1. 安装布局与进程

发布形式是压缩包（`tools/build_release.py` 生成），由包内只用系统 Python 标准库的 `maestroctl` 安装和维护（U-29）。根 `pyproject.toml` 是权威版本，服务运行时从安装元数据读取，SDK 包声明与契约由准备入口同步；Runner 独立版本不变。维护者用 `tools/release.py` 从固定 dev 提交准备正式候选、发布说明与下一开发版本，最终 SHA 完整检查后才快进 master 并创建固定附注 tag（U-66）。

所有分支 push 检查源码，PR 复用对应提交结果；正式 tag 读取准确 SHA 的成功 `verify` 任务及必需步骤，独立 `publish` 任务构建和验证实际 wheel，上传失败不改变源码检查结论（U-67）。dev 通过后发 Gitea 预览，正式 tag 发 Gitea Release 与同版本 SDK 包；GitHub 从相同 tag 重建并验证 Release，再更新 Pages 正式 SDK 索引（U-42、U-62）。预览 Release 与 `.dev<CI运行号>` SDK 各留最近 10 个，正式版不清理。

包附 SDK wheel、许可证清单与 `SHA256SUMS`，manifest 固定源码 SHA 与文件摘要；构建输入锁定、归档元数据规范化，同一输入可重建。发布先准备草稿，附件齐全后公开；缺失补传，同摘要跳过，异摘要或 tag 改指向则停止。两端附件、SDK 包仓库和 Pages 全部核对后，正式准备与下一开发版本一起推回 dev，保留并发提交。各渠道没有原子事务，入口按实际状态续跑，不把部分成功报告为完成。操作见[发布指南](../guides/releasing.md)。不自动部署：管理员在管理页检查并发起正式版更新，或在主机上执行 `maestroctl update`（第 5 节）。

布局是系统级的（U-13）：

~~~text
/opt/maestro/versions/<build>/      程序（服务 venv，网页在服务包内）、该版本的 maestroctl、RELEASE.json
/opt/maestro/versions/<build>/runner-env -> ../../harbor/<harbor-version>/<runner-revision>
/opt/maestro/current                指向 versions/<build> 的链接
/opt/maestro/harbor/<harbor-version>/<runner-revision>/venv/  锁定 Harbor 与 Runner 的环境
/opt/maestro/bin/uv、python/        安装与更新用的 uv；主机没有合适 Python 时 uv 下载的解释器
/etc/maestro/config.toml            部署配置（root 所有，maestro 组可读）
/etc/maestro/environment            服务进程的代理与 DOCKER_HOST
/etc/maestro/install.json           安装记录（版本、时间、上一版本）
/etc/maestro/secrets/               按代次保存的主密钥文件，只有服务账号可读
/var/lib/maestro/app.db             业务数据库，WAL / SHM 同目录
/var/lib/maestro/artifacts/         原始产物
/var/lib/maestro/attachments/       附件的文件内容（blobs/）与快照清单（snapshots/）
/var/lib/maestro/registry/          本机镜像仓库的存储：准备好的镜像（U-17、U-55；是数据，不是缓存）
/var/lib/maestro/work/              Runner 工作目录，归档为产物前的暂存
/var/lib/maestro/exports/           临时导出包
/var/lib/maestro/home/              服务账号的家目录（Docker 客户端与 buildx 的状态）
/var/lib/maestro/update/            网页发起的更新：服务写的请求、更新任务写回的状态（第 5 节）
/var/cache/maestro/tasks/           按上游内容摘要保存的任务材料
/var/backups/maestro/<时间>/        备份集（含更新前的自动备份）
journald                            服务标准输出 / 错误
~~~

服务以专用系统账户 `maestro` 运行，一个 systemd 单元包含 API、调度器和进程内 Worker。服务环境与 Harbor / Runner 环境由 uv 按各自锁文件分别安装，不共享依赖；Harbor 版本下再按 Runner 修订细分，同版 Harbor 的包装器更新不会覆盖旧版本仍在用的环境。

安装器把 `maestro` 加入系统 Docker 套接字所属的组。这个组约等于 root，在可信团队前提下接受（U-29）；专用账户使用自己的 rootless Docker 是以后的备选。Docker 能力只由平台使用，不挂进任务容器。

管理入口是命令行 `maestroctl`：check、install、status、start / stop / restart、logs、backup、restore、update、registry-gc、uninstall、version。网页只发起一种维护动作：更新到获取源里比当前新的正式版，由 root 的更新任务执行（第 5 节，U-96）；其余维护只在命令行。

**本机镜像仓库**（N-01、U-56）：准备好的镜像存在 Docker 官方 `registry` 镜像（按 digest 固定版本）运行的容器 `maestro-registry` 里，只绑定 `127.0.0.1`（默认端口 8425，安装时 `--registry-port` 指定，写入配置 `image_registry`），重启策略 `unless-stopped`，以服务账户身份运行、数据挂在 `/var/lib/maestro/registry`。Docker 默认信任 `127.0.0.0/8` 上的 HTTP 仓库，不改守护进程配置。`install` 与 `update` 在启动服务前创建或重建这个容器；`status` 与 `check` 报告它是否应答；`uninstall` 删除容器、保留数据，`--purge` 连数据一起删。

`maestroctl registry-gc` 释放不再被任何镜像清单引用的层（物理删除数据集版本后）：把仓库切到只读（拉取照常、推送被拒），执行官方的垃圾回收，再恢复可写。有导入、首次准备或恢复在进行时拒绝（它们可能要推送），`--force` 例外；运行可以照常进行。

## 2. 安装与首次启动

| 编号 | 要求 |
| --- | --- |
| OPS-01 | `check` 与 `install` 第一步检查 Linux / systemd、uv 与 Python 3.12～3.13、Docker 守护进程及 compose（2.24 以上，覆盖文件要用 `!reset`）/ buildx、Docker 套接字、内核 nf_tables、数据盘余量、服务与镜像仓库的端口；已安装时另查镜像仓库是否应答；失败项给出修复动作并中止 |
| OPS-02 | 程序、配置、数据、缓存、备份分目录安装，版本目录不混入业务数据 |
| OPS-03 | 注册 systemd 单元，生成配置与主密钥；一次性初始化令牌只在安装输出中显示一次 |
| OPS-04 | 每次启动先对账，完成前不受理（第 4 节） |
| OPS-05 | 卸载默认保留数据与配置，彻底删除需列出范围并确认实例编号（第 7 节） |
| OPS-06 | 未满足：兼容信息只有发布包的 `RELEASE.json`（版本、提交、数据库结构版本、Harbor 版本、Runner 修订、文件摘要），没有经验证的发行版与 CPU 架构支持清单 |
| OPS-07 | 同机更新带自动备份与失败回退；网页发起的更新先排空（第 5 节，U-96） |

安装检查不改全机 Docker 配置（守护进程没有代理时只提醒）。nftables 只查内核模块；网络收紧是否真正生效由服务启动时经 Runner 探测（第 4 节）。

再次 `install` 到已有数据上会沿用原配置、密钥和数据，不再发初始化令牌。`restart` 只启动 `current` 指向的已装版本，从不检查更新或下载。

## 3. 配置

部署配置是 `/etc/maestro/config.toml`；全部配置项、默认值与取值范围以 `src/maestro/config.py` 为准，生效值见 `/api/v1/capabilities` 的 `limits`（团队策略与结构性常量也在其中）。数值分三类（U-21）：

- **与机器和磁盘有关的**（监听地址、目录、本机镜像仓库地址、执行容量、证据与导出上限、构建 / 拉取 / 推送时限、同时恢复数、Hub 并发、Runner 心跳与静默时限、磁盘水位）放在 `config.toml`。
- **团队策略**（PAT、运行规模、保留期、登录限流等）在数据库中按修订保存，由管理页“团队策略”修改，未修改前是内置初值（修订 0，`src/maestro/domain/limits.py`）；提供商并发在“容量”页。新值对之后的提交生效，已受理运行不变。
- **结构性常量**（协议、分页、批大小等）写死在代码里。

部署的网络条件（安装网络，下文）不在这三类里：它也取决于主机，但由管理员在网页按版本维护，不需要重启。

要点：

- `config.toml` 的任何改动都要重启服务才生效。未知键、类型错误、越界与相互矛盾的值（例如单文件证据上限大于单次尝试总量、默认构建时限大于上限、静默时限小于 3 个心跳间隔）都会拒绝启动并指出是哪一项；合法但可能不是本意的值（例如导出上限高于停止派发水位）在启动日志中提醒；已不再使用的键（`config.py` 的 `RETIRED`，如 `setup_proxy`）不拒绝启动，只被忽略并在启动日志中说明。
- `master_key_dir` 必须在 `data_dir` 之外：备份复制的是 `data_dir`，主密钥要单独保管。
- `cookie_secure` 默认开启；纯 HTTP 访问时浏览器不回传会话 cookie，须关闭它或经 HTTPS 访问。
- 管理页“安装网络”分区下方只读显示 `config.toml` 的生效值、已忽略的旧键，以及服务进程环境里是否有代理。

安装网络（管理页“安装网络”，`src/maestro/domain/setup_network.py`）说明本机安装软件时经什么网络：

- **代理**与**不走代理**的地址交给平台的镜像构建（Docker 预定义的代理构建参数，不进镜像，不改准备方式摘要）和运行 setup 中的安装（Harness 自身安装与附件脚本）。
- **安装变量**（例如 `npm_config_registry`、`NVM_NODEJS_ORG_MIRROR`、`PIP_INDEX_URL` 指定的包镜像源）只交给运行 setup 中的安装，不交给镜像构建（Dockerfile 只看得到自己声明的参数）。
- **Harness 安装时限**设了就取代 Harness 自己的 setup 时限（Harbor 多数 agent 为 360 秒），带附件时再加附件的安装与复制时间。
- Agent 与验证阶段从不获得这些设置。Oracle 运行没有 setup 工作，也不获得。
- 每次保存是一个完整的新版本（第 0 版为什么都没设置），带修改人、时间与说明。准备与执行在**开始时**读当前版本，不在受理时冻结，所以换掉失效的镜像源后，排队中的运行与重跑也能用上；每个执行尝试和每次镜像构建都记下用的是第几版，版本内容团队成员都能看到。它是部署的网络条件，不进运行配置与 `config_digest`。
- 不能含凭据，因为 Harbor 以 `docker exec -e` 传变量，本机进程列表人人可见，而且成员能看到全部内容：代理只接受 `http(s)://主机:端口`，不带用户名密码，不接受 SOCKS；变量名须是普通 shell 变量名，代理变量只能在代理一栏设置，`MAESTRO_` 开头的名字留给平台，名字像凭据（含 token、secret、password、auth、key 等）或值里带 `user:password@` 的保存时拒绝。代理写成 127.0.0.1 / localhost 时页面提醒，容器里那是容器自己。
- 镜像源只解决下载慢或不稳，不是缓存：每个试次仍下载一遍。要在本机缓存，由部署方运行缓存代理（apt 用 squid 一类设为代理）或缓存型 registry（npm 用 Verdaccio 一类填进安装变量），平台不另外支持。
- 拉取镜像走 Docker 守护进程自己的代理与 registry 镜像源，`maestroctl check` 报告它们，不代为修改。
- Codex、Claude Code 与 OpenCode 都用 npm 安装（Node 由 nvm 装，musl 镜像用系统包）：nvm 脚本来自 GitHub，需要代理；Node 与 npm 包可走 `NVM_NODEJS_ORG_MIRROR` 与 `npm_config_registry` 指定的镜像源。Harbor 0.24.0 的 Codex 安装命令写死 `NVM_NODEJS_ORG_MIRROR`，平台注入的变量优先级更高。管理页的上游版本提示也经这里的代理与 npm 镜像源查询。

已知限制：

- `root_path` 可配置，但网页写死 `/api/v1` 与根路径路由（`frontend/src/lib/api.ts`），子路径部署目前不能用。
- 部署配置在网页上只读，`config.toml` 仍须在主机上修改并重启。
- 更新的获取源（`update_repository`，GitHub 的 `owner/name`，默认 `au12321ua/maestro`）与是否检查更新（`update_check`）只在 `config.toml` 里设置：获取源决定 root 的更新任务执行哪份代码，网页不能改（U-96）。

## 4. systemd 与启动对账

`maestro.service` 以 `maestro` 账户运行 `current` 版本的 `maestro serve`，`Restart=on-failure` 并限制启动速率（经 `maestroctl` 的启动会先清除计数），带 `NoNewPrivileges`、`ProtectSystem=full`（只放开主密钥目录）等加固；完整单元见 `ops/maestroctl.py` 的 `render_unit`。单元文件在更新时重写，本地改动应放在 `systemctl edit` 的覆盖文件里。

单实例锁保证第二个实例不能同时写同一数据库、管理同一组容器。停止时 Runner 随服务收到 SIGTERM 并停止各自的 Harbor Job；遗留资源由下次启动按标签清理。

启动顺序：读取配置与主密钥 → 检查数据库结构版本 → 对账存储文件并启动维护循环 → 经 Runner 探测本机能否限制任务网络 → Worker 重启对账（在执行的运行标为中断，已得评分保留，按标签清理本实例遗留容器，见 [05](05-scheduling-execution.md)）→ 开始服务请求。结构版本不兼容时 `/health/ready` 报告 `maintenance`，不启动 Worker 与维护循环，只保留读取与诊断。`accepting_runs` 另外要求主密钥可用、Worker 在跑且磁盘水位正常。

网络探测结论为不能限制时，模型运行在受理时即被拒绝（阻塞项 `NETWORK_POLICY_UNSUPPORTED`，不扣额度），能力接口的 `features.restricted_network` 报告结果；探测失败按不能处理，Runner 在每个运行开始时的检查仍是最后一道。

`stop` / `restart` 不排空：在途工作被中断并如实记为中断。排空只用于网页发起的更新（第 5 节）。

## 5. 版本更新

### 命令行

`maestroctl update <包>`（同一台主机，U-29）：

1. 包里的 `maestroctl` 与当前执行的不是同一版本时，交给新包的 `maestroctl` 执行；目标已是当前版本时直接结束。
2. 在当前版本旁边装好新版本，服务照常运行；Harbor 与 Runner 修订不变时复用已有环境。
3. 用旧版本的 `maestro activity` 查询在途工作：有执行中的运行，或进行中的导入、首次准备等 Operation 时拒绝更新；加 `--interrupt-running` 则让它们按重启语义中断（已得评分保留）。排队中的运行与导出不算在途，留到新版本启动后继续。
4. 停服务，用旧版本做数据库备份（`/var/backups/maestro/<时间>-before-<新版本>`，不含业务文件）。
5. 用新版本迁移数据库，原子切换 `current`，重写单元文件，重建镜像仓库容器（数据不动），启动并等待 `/health/ready` 为 ready。
6. 迁移、启动或就绪任何一步失败：停服务，换回旧版本与旧单元文件，用第 4 步的备份恢复数据库，启动旧版本，以非零状态退出。新版本在失败前短暂写入的数据随之丢弃。
7. 成功后更新安装记录，保留上一个版本供手动回退，删除更早的版本和不再被引用的 Runner 环境。

回退必须连同数据库一起恢复：迁移可能与旧程序不兼容，只切换 `current` 不算回滚。新版本不改写共享 Harbor 环境或旧版本的锁文件。

`maestroctl update --release <版本>` 省去手动下载：按 `config.toml` 的获取源取该版本的 GitHub Release（拒绝草稿与预发布），下载发布包与 `SHA256SUMS` 并核对摘要，确认版本比当前新（不降级、不重装），再交给包里的 `maestroctl` 走上面的流程。访问 GitHub 走服务的代理（`/etc/maestro/environment`），下载 Python 包仍按 `--download-proxy`。终端里的更新不排空，有在途工作时照旧拒绝或按 `--interrupt-running` 中断。

### 网页发起（U-96）

服务每天查询一次获取源最新的正式版（GitHub `releases/latest`，经服务进程的代理），管理员也可以在管理页“更新”中立即检查；只有比当前安装版本新的正式版才提示，所以装着开发预览（`.dev`）的实例在下一个正式版出来前不会被提示降级。页面显示发布说明，标题含“破坏性变更”“迁移”“更新后”“首次准备”等字样的段落列在前面。查询失败只显示原因，保留上一次成功查到的结果。`update_check = false` 时不查询，网页没有更新入口（`features.web_update`）。开发预览只走命令行。

1. 管理员确认发布说明后发起“更新到 X”（只能是最近一次检查查到的那个版本），服务进入**待更新**。待更新是数据库里的一条记录（`service_updates`），重启后仍然有效：运行照常受理和排队，但不派发，排队原因 `UPDATE_PENDING`（[05](05-scheduling-execution.md) 第 3 节）；新的导入、首次准备与回收缓存拒绝启动（`UPDATE_PENDING`），已开始的继续做完。所有成员看到横幅，管理页列出还在执行的运行与 Operation 及其提交者。
2. 管理员可以随时取消待更新，恢复派发；也可以“立即更新”，在途工作按重启语义中断，已得评分保留（与 `--interrupt-running` 相同）。
3. 在途工作——执行中的运行，以及导入、首次准备等 Operation（与 `maestro activity` 同一定义）——全部结束，或选了立即更新后，服务把请求原子写入 `/var/lib/maestro/update/request.json`：格式版本、请求编号、目标版本、是否中断、操作者与时间。服务只能写版本号，不能给下载地址或文件。
4. root 的 `maestro-update.path` 监视这个文件，触发一次性的 `maestro-update.service`，执行当前版本的 `maestroctl update --request`：先把请求改名占下（此后取消会被拒绝，`UPDATE_IN_PROGRESS`），严格校验内容（不跟随符号链接、大小与字段格式），再走 `--release` 的流程；没有交互的这条路径下载 Python 包也走服务的代理。更新任务不挂在终端或服务进程上，断线、关掉浏览器或服务停止都不影响它。
5. 进度与结果写进同目录的 `status.json`（`running` 的下载 / 安装阶段，最后 `succeeded` 或 `failed`，带是否已回滚与最后一条错误），详细输出进 journald 的 `maestro-update` 单元。服务读到本次请求的最终结果就结束待更新、恢复派发：成功时由新版本读到，失败回滚后由旧版本读到（更新前的备份里同样有这条待更新）；状态停在 `running` 而更新任务的进程已不存在时按失败结束。

`maestroctl install` 与 `update` 写入并启用这两个单元，`uninstall` 删除；更新失败回滚时单元文件一起换回。`check` 报告路径单元是否在监视，`status` 报告它和最近一次网页更新的结果。

安全边界：服务能做的只是“请求更新到获取源里比当前新的某个正式版”，获取源与是否开启都在 root 所有的配置里。`SHA256SUMS` 与发布包来自同一个 Release，只防传输损坏，不防发布源本身被篡改，信任落在 GitHub 仓库与发布流程上。`maestro` 账户在 Docker 组里，本来约等于 root（U-29），这套机制不明显扩大服务被攻破时的影响；更新任务以 root 读取服务可写目录里的文件，所以只改名、不跟随链接，状态文件经临时文件替换写入。

数据库迁移在单个事务里执行，失败整体回滚。重建被其他表引用的表时，开启外键检查会让替换父表的语句因历史引用而失败，所以迁移连接在事务开始前临时关闭外键检查，提交前对整个数据库做一次外键校验，有悬空引用（包括迁移前就存在的）则回滚并让升级失败。成功或失败后都恢复外键检查，日常服务连接始终强制检查。迁移期间外键关闭，`ON DELETE CASCADE` 与 `SET NULL` 不会触发，迁移须自行处理子表数据，漏掉的悬空行会被提交前的校验拦下。

已知限制：终端里的 `maestroctl update` 不排空；`stop`、`restart` 与 `restore` 也没有维护模式。发布包只核对与它同一 Release 的 `SHA256SUMS`，没有构建来源证明。

## 6. 备份与恢复

`maestroctl backup [--include-files] [--label …]` 调用 `maestro backup`，写入 `/var/backups/maestro/<时间>[-label]/`，服务可以继续运行：

- 数据库经 SQLite 备份接口取一致快照，转为单文件并做完整性检查；不直接复制活动主库（会漏掉 WAL）。
- 复制部署配置；带 `--include-files` 时另复制附件、镜像仓库的存储与产物。`work`、`exports` 与 `cache_dir` 不备份（暂存，或可按需重建）。仓库在备份时照常运行；它的文件按内容寻址，正在推送的镜像可能不完整，恢复后由首次准备或恢复重新推送。
- 最后原子写入 `manifest.json`：格式版本、实例、数据库结构版本、所需主密钥的代次与文件名、数据库摘要、各业务目录的文件数与字节数、未包含项。没有清单的目录就是不完整的备份。
- 主密钥不进备份集。主密钥丢失意味着加密凭据不可恢复，只能重新填写；不能用新主密钥声称恢复了旧凭据。

备份不加读 pin：快照之后被回收的文件不在备份里，恢复后由启动对账标为缺失；快照不认识的文件按未索引处理。

`maestroctl restore <目录> [--include-files] [--allow-missing-key]` 先停服务，单实例锁保证服务确实已停。所有检查都在动现场之前：清单与数据库摘要一致；结构版本是本版本已知的（否则要求用做出备份的版本恢复）；所需主密钥在 `master_key_dir`（否则拒绝，除非显式接受凭据失效）。然后替换数据库（清除旧 WAL / SHM），按需替换业务文件（原目录改名为 `<名>.before-restore-<时间>` 保留；替换了文件时镜像仓库容器在新文件上重建），用当前版本迁移；服务原来在运行时再启动并等待就绪。备份时在执行的运行由启动对账标为中断。

已知限制：只支持恢复到同一台主机；恢复后排队工作不暂停，直接继续派发。换机恢复见 [N-21](../proposals/N-21-offhost-restore.md)。

## 7. 卸载与目录边界

`maestroctl uninstall` 停止并删除服务单元、`/opt/maestro` 与命令链接，保留配置、主密钥、数据、缓存、备份与账户；`--remove-cache` 另删缓存。

`--purge` 先列出各目录的大小与影响、本实例的 Docker 容器 / 网络 / 卷 / 镜像数（按 `io.maestro.instance_id` 标签和 `<仓库地址>/maestro-prep/<实例>` 镜像名查找）与账户，不删除；只有带上 `--confirm <实例编号>` 才执行。删除只针对布局里固定的目录，拒绝符号链接和布局之外的路径；不清理全机 Docker 资源，不动 journald。

## 8. 可观测性

服务日志是 uvicorn 与 Python logging 的文本输出，写入 journald，按单元查询（`maestroctl logs`）；保留时间与空间由宿主 journald 策略决定，应用不改全机策略。request_id 在响应头 `X-Request-Id`、响应 meta 和审计事件中。业务审计在数据库，不随日志轮转。试次日志、Agent 轨迹与结果文件作为产物保存（见 [07](07-artifacts-export.md)），保存前做基本脱敏。

`maestroctl status` 汇总版本、单元状态、就绪状态、运行数与磁盘余量。`/health/ready` 只报告是否就绪与是否受理运行；需登录的 `/api/v1/system-status` 给出结构版本、主密钥、Worker、执行器、磁盘水位等明细。

已知限制：服务日志没有结构化字段（run_id、trial_id、phase 等）与脱敏过滤；没有指标导出与外部告警。

## 9. 验收

- 安装、初始化、本机镜像仓库容器、启停、日志、更新、失败回退、网页发起的更新（伪造的 GitHub 接口、路径单元、排空、摘要不符的失败）、同机备份恢复、保留数据卸载与彻底删除：`tests/maestro/live/test_install.py`（需 `MAESTRO_INSTALL_TESTS=1`，在临时根目录以 systemd 用户单元运行）；布局、单元文件、请求与状态文件、Release 解析与删除边界：`tests/maestro/ops/test_maestroctl.py`；备份集与恢复检查：`tests/maestro/ops/test_backup.py`；版本比较、发布说明、待更新期间的派发与 Operation、请求与结果、取消与立即更新、权限：`tests/maestro/ops/test_web_update.py`。
- 非正常终止后按规则标记中断、保留评分并清理本实例遗留容器：见 [13](13-testing-plan.md) 的 F-08。
- 普通重启不更新版本。
- 异机恢复不在当前范围，见 [N-21](../proposals/N-21-offhost-restore.md)。
