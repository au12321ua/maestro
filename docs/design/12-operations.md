# 12 部署运维与可观测性

导航：[总览](00-overview.md) · 依赖：[执行](05-scheduling-execution.md)、[存储](07-artifacts-export.md)、[权限](09-identity-security.md) · 相关代码：`ops/maestroctl.py`、`tools/build_release.py`、`src/maestro/backup.py`、`src/maestro/config.py`；操作步骤见[部署指南](../guides/deployment.md)

## 1. 安装布局与进程

发布形式是压缩包（`tools/build_release.py` 生成），由包内只用系统 Python 标准库的 `maestroctl` 安装和维护（U-29）。布局是系统级的（U-13）：

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
/var/lib/maestro/attachments/       已发布附件的原件
/var/lib/maestro/preparations/      准备原件（镜像归档，U-17；是数据，不是缓存）
/var/lib/maestro/work/              Runner 工作目录，归档为产物前的暂存
/var/lib/maestro/exports/           临时导出包
/var/lib/maestro/home/              服务账号的家目录（Docker 客户端与 buildx 的状态）
/var/cache/maestro/tasks/           按上游内容摘要保存的任务材料
/var/backups/maestro/<时间>/        备份集（含更新前的自动备份）
journald                            服务标准输出 / 错误
~~~

服务以专用系统账户 `maestro` 运行，一个 systemd 单元包含 API、调度器和进程内 Worker。服务环境与 Harbor / Runner 环境由 uv 按各自锁文件分别安装，不共享依赖；Harbor 版本下再按 Runner 修订细分，同版 Harbor 的包装器更新不会覆盖旧版本仍在用的环境。

安装器把 `maestro` 加入系统 Docker 套接字所属的组。这个组约等于 root，在可信团队前提下接受（U-29）；专用账户使用自己的 rootless Docker 是以后的备选。Docker 能力只由平台使用，不挂进任务容器。

管理入口只有命令行 `maestroctl`：check、install、status、start / stop / restart、logs、backup、restore、update、uninstall、version。网页不发起维护动作（U-29）。

## 2. 安装与首次启动

| 编号 | 要求 |
| --- | --- |
| OPS-01 | `check` 与 `install` 第一步检查 Linux / systemd、uv 与 Python 3.12～3.13、Docker 守护进程及 compose / buildx、Docker 套接字、内核 nf_tables、数据盘余量与端口；失败项给出修复动作并中止 |
| OPS-02 | 程序、配置、数据、缓存、备份分目录安装，版本目录不混入业务数据 |
| OPS-03 | 注册 systemd 单元，生成配置与主密钥；一次性初始化令牌只在安装输出中显示一次 |
| OPS-04 | 每次启动先对账，完成前不受理（第 4 节） |
| OPS-05 | 卸载默认保留数据与配置，彻底删除需列出范围并确认实例编号（第 7 节） |
| OPS-06 | 未满足：兼容信息只有发布包的 `RELEASE.json`（版本、提交、数据库结构版本、Harbor 版本、Runner 修订、文件摘要），没有经验证的发行版与 CPU 架构支持清单 |
| OPS-07 | 同机更新带自动备份与失败回退（第 5 节）；排空见 [N-12](../proposals/N-12-drain-update.md) |

安装检查不改全机 Docker 配置（守护进程没有代理时只提醒）。nftables 只查内核模块；网络收紧是否真正生效由服务启动时经 Runner 探测（第 4 节）。

再次 `install` 到已有数据上会沿用原配置、密钥和数据，不再发初始化令牌。`restart` 只启动 `current` 指向的已装版本，从不检查更新或下载。

## 3. 配置

部署配置是 `/etc/maestro/config.toml`；全部配置项、默认值与取值范围以 `src/maestro/config.py` 为准，生效值见 `/api/v1/capabilities` 的 `limits`（团队策略与结构性常量也在其中）。数值分三类（U-21）：

- **与机器和磁盘有关的**（监听地址、目录、执行容量、证据与导出上限、构建 / 拉取 / 保存时限、同时恢复数、Hub 并发、Runner 心跳与静默时限、磁盘水位、准备阶段代理）放在 `config.toml`。
- **团队策略**（PAT、运行规模、保留期、登录限流等）在数据库中按修订保存，由管理页“团队策略”修改，未修改前是内置初值（修订 0，`src/maestro/domain/limits.py`）；提供商并发在“容量”页。新值对之后的提交生效，已受理运行不变。
- **结构性常量**（协议、分页、批大小等）写死在代码里。

要点：

- `config.toml` 的任何改动都要重启服务才生效。未知键、类型错误、越界与相互矛盾的值（例如单文件证据上限大于单次尝试总量、默认构建时限大于上限、静默时限小于 3 个心跳间隔）都会拒绝启动并指出是哪一项；合法但可能不是本意的值（例如导出上限高于停止派发水位、准备代理写成 127.0.0.1）在启动日志中提醒。
- `master_key_dir` 必须在 `data_dir` 之外：备份复制的是 `data_dir`，主密钥要单独保管。
- `cookie_secure` 默认开启；纯 HTTP 访问时浏览器不回传会话 cookie，须关闭它或经 HTTPS 访问。
- 准备阶段代理（`setup_proxy`）只交给平台的镜像构建和运行 setup 中的安装脚本，Agent 与验证阶段从不获得；不能带用户名或密码，因为 Harbor 以 `docker exec -e` 传变量，进程列表人人可见。拉取镜像走 Docker 守护进程自己的代理设置。

已知限制：

- `root_path` 可配置，但网页写死 `/api/v1` 与根路径路由（`frontend/src/lib/api.ts`），子路径部署目前不能用。
- 网页不显示部署配置本身，只能经 `/capabilities` 看到生效上限。

## 4. systemd 与启动对账

`maestro.service` 以 `maestro` 账户运行 `current` 版本的 `maestro serve`，`Restart=on-failure` 并限制启动速率（经 `maestroctl` 的启动会先清除计数），带 `NoNewPrivileges`、`ProtectSystem=full`（只放开主密钥目录）等加固；完整单元见 `ops/maestroctl.py` 的 `render_unit`。单元文件在更新时重写，本地改动应放在 `systemctl edit` 的覆盖文件里。

单实例锁保证第二个实例不能同时写同一数据库、管理同一组容器。停止时 Runner 随服务收到 SIGTERM 并停止各自的 Harbor Job；遗留资源由下次启动按标签清理。

启动顺序：读取配置与主密钥 → 检查数据库结构版本 → 对账存储文件并启动维护循环 → 经 Runner 探测本机能否限制任务网络 → Worker 重启对账（在执行的运行标为中断，已得评分保留，按标签清理本实例遗留容器，见 [05](05-scheduling-execution.md)）→ 开始服务请求。结构版本不兼容时 `/health/ready` 报告 `maintenance`，不启动 Worker 与维护循环，只保留读取与诊断。`accepting_runs` 另外要求主密钥可用、Worker 在跑且磁盘水位正常。

网络探测结论为不能限制时，模型运行在受理时即被拒绝（阻塞项 `NETWORK_POLICY_UNSUPPORTED`，不扣额度），能力接口的 `features.restricted_network` 报告结果；探测失败按不能处理，Runner 在每个运行开始时的检查仍是最后一道。

计划停机没有排空：`stop` / `restart` 会中断在途工作并如实记为中断。带排空的维护见 [N-12](../proposals/N-12-drain-update.md)。

## 5. 版本更新

`maestroctl update <包>`（同一台主机，命令行，U-29）：

1. 包里的 `maestroctl` 与当前执行的不是同一版本时，交给新包的 `maestroctl` 执行；目标已是当前版本时直接结束。
2. 在当前版本旁边装好新版本，服务照常运行；Harbor 与 Runner 修订不变时复用已有环境。
3. 用旧版本的 `maestro activity` 查询在途工作：有执行中的运行，或进行中的导入、首次准备等 Operation 时拒绝更新；加 `--interrupt-running` 则让它们按重启语义中断（已得评分保留）。排队中的运行与导出不算在途，留到新版本启动后继续。
4. 停服务，用旧版本做数据库备份（`/var/backups/maestro/<时间>-before-<新版本>`，不含业务文件）。
5. 用新版本迁移数据库，原子切换 `current`，重写单元文件，启动并等待 `/health/ready` 为 ready。
6. 迁移、启动或就绪任何一步失败：停服务，换回旧版本与旧单元文件，用第 4 步的备份恢复数据库，启动旧版本，以非零状态退出。新版本在失败前短暂写入的数据随之丢弃。
7. 成功后更新安装记录，保留上一个版本供手动回退，删除更早的版本和不再被引用的 Runner 环境。

回退必须连同数据库一起恢复：迁移可能与旧程序不兼容，只切换 `current` 不算回滚。新版本不改写共享 Harbor 环境或旧版本的锁文件。

已知限制：有运行在跑时只能等待或中断，没有排空、维护模式和断线后可查询的维护记录，见 [N-12](../proposals/N-12-drain-update.md)。

## 6. 备份与恢复

`maestroctl backup [--include-files] [--label …]` 调用 `maestro backup`，写入 `/var/backups/maestro/<时间>[-label]/`，服务可以继续运行：

- 数据库经 SQLite 备份接口取一致快照，转为单文件并做完整性检查；不直接复制活动主库（会漏掉 WAL）。
- 复制部署配置；带 `--include-files` 时另复制附件、准备原件与产物。`work`、`exports` 与 `cache_dir` 不备份（暂存，或可按需重建）。
- 最后原子写入 `manifest.json`：格式版本、实例、数据库结构版本、所需主密钥的代次与文件名、数据库摘要、各业务目录的文件数与字节数、未包含项。没有清单的目录就是不完整的备份。
- 主密钥不进备份集。主密钥丢失意味着加密凭据不可恢复，只能重新填写；不能用新主密钥声称恢复了旧凭据。

备份不加读 pin：快照之后被回收的文件不在备份里，恢复后由启动对账标为缺失；快照不认识的文件按未索引处理。

`maestroctl restore <目录> [--include-files] [--allow-missing-key]` 先停服务，单实例锁保证服务确实已停。所有检查都在动现场之前：清单与数据库摘要一致；结构版本是本版本已知的（否则要求用做出备份的版本恢复）；所需主密钥在 `master_key_dir`（否则拒绝，除非显式接受凭据失效）。然后替换数据库（清除旧 WAL / SHM），按需替换业务文件（原目录改名为 `<名>.before-restore-<时间>` 保留），用当前版本迁移；服务原来在运行时再启动并等待就绪。备份时在执行的运行由启动对账标为中断。

已知限制：只支持恢复到同一台主机；恢复后排队工作不暂停，直接继续派发。换机恢复见 [N-21](../proposals/N-21-offhost-restore.md)。

## 7. 卸载与目录边界

`maestroctl uninstall` 停止并删除服务单元、`/opt/maestro` 与命令链接，保留配置、主密钥、数据、缓存、备份与账户；`--remove-cache` 另删缓存。

`--purge` 先列出各目录的大小与影响、本实例的 Docker 容器 / 网络 / 镜像数（按 `io.maestro.instance_id` 标签和 `maestro-prep/<实例>` 仓库名查找）与账户，不删除；只有带上 `--confirm <实例编号>` 才执行。删除只针对布局里固定的目录，拒绝符号链接和布局之外的路径；不清理全机 Docker 资源，不动 journald。

## 8. 可观测性

服务日志是 uvicorn 与 Python logging 的文本输出，写入 journald，按单元查询（`maestroctl logs`）；保留时间与空间由宿主 journald 策略决定，应用不改全机策略。request_id 在响应头 `X-Request-Id`、响应 meta 和审计事件中。业务审计在数据库，不随日志轮转。试次日志、Agent 轨迹与结果文件作为产物保存（见 [07](07-artifacts-export.md)），保存前做基本脱敏。

`maestroctl status` 汇总版本、单元状态、就绪状态、运行数与磁盘余量。`/health/ready` 只报告是否就绪与是否受理运行；需登录的 `/api/v1/system-status` 给出结构版本、主密钥、Worker、执行器、磁盘水位等明细。

已知限制：服务日志没有结构化字段（run_id、trial_id、phase 等）与脱敏过滤；没有指标导出与外部告警。

## 9. 验收

- 安装、初始化、启停、日志、更新、失败回退、同机备份恢复、保留数据卸载与彻底删除：`tests/maestro/test_install.py`（需 `MAESTRO_INSTALL_TESTS=1`，在临时根目录以 systemd 用户单元运行）；布局、单元文件与删除边界：`tests/maestro/test_maestroctl.py`；备份集与恢复检查：`tests/maestro/test_backup.py`。
- 非正常终止后按规则标记中断、保留评分并清理本实例遗留容器：见 [13](13-testing-plan.md) 的 F-08。
- 普通重启不更新版本。
- 带在途工作的排空更新与异机恢复不在当前范围，见 [N-12](../proposals/N-12-drain-update.md)、[N-21](../proposals/N-21-offhost-restore.md)。
