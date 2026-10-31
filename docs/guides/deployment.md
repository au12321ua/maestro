# 部署与维护 Maestro

本页是发布包里的 `INSTALL.md`，也在仓库 `docs/guides/deployment.md`。设计依据见[运维设计](../design/12-operations.md)（仓库内）；部署形态是用户决定的系统级布局（决策记录 U-13、U-29）。

## 装出来是什么样

一台 Linux 主机上装一个实例，由管理命令 `maestroctl` 安装和维护：

| 位置 | 内容 | 归属 |
| --- | --- | --- |
| `/opt/maestro/versions/<版本>/` | 程序、网页、独立的 Python 环境、该版本的 `maestroctl` | root，只读 |
| `/opt/maestro/current` | 指向当前版本的链接；服务从这里启动 | root |
| `/opt/maestro/harbor/<Harbor 版本>/<Runner 修订>/` | 锁定版本的 Harbor 与 Runner 环境，多个版本可共用 | root，只读 |
| `/etc/maestro/config.toml`、`environment` | 部署配置；服务进程的代理等环境变量 | root，`maestro` 组可读 |
| `/etc/maestro/secrets/` | 主密钥，只有 `maestro` 能读 | `maestro`，0700 |
| `/var/lib/maestro/` | 数据库、产物、附件；`registry/` 是本机镜像仓库的存储（准备好的镜像），`home/` 是服务账户的家目录 | `maestro`，0700 |
| `maestro-registry` 容器 | 本机镜像仓库，只监听 `127.0.0.1:8425`，由 Docker 随守护进程重启 | 以 `maestro` 身份运行 |
| `/var/cache/maestro/` | 可重建的任务材料缓存 | `maestro`，0700 |
| `/var/backups/maestro/` | `maestroctl backup` 与更新前的自动备份 | `maestro`，0700 |
| `maestro.service` | systemd 系统服务；日志进 journald | — |
| `/usr/local/sbin/maestroctl` | 指向当前版本的 `maestroctl` | — |

服务以专用系统账户 `maestro` 运行，由安装器创建并加入系统 Docker 套接字所属的组（通常是 `docker`）。**这个组约等于 root**：服务一旦被攻破，攻击者可以借 Docker 拿到整机权限。这是在“受邀的可信团队”前提下接受的取舍；以后可增加“专用账户使用自己的 rootless Docker”的备选。

## 主机要求

- 带 systemd 的 Linux（x86_64 或 aarch64）；以 root 运行 `maestroctl`。
- Docker Engine 正在运行，带 compose（2.24 以上）与 buildx 插件；内核有 nf_tables（Harbor 用它限制任务容器的网络；没有时模型运行在受理时就被拒绝，Oracle 运行不受影响）。
- [uv](https://docs.astral.sh/uv/)。Python 3.12 或 3.13：主机没有时 uv 会下载到 `/opt/maestro/python`。安装需要访问 PyPI，可经 `--proxy`。
- 数据目录所在磁盘至少 20 GiB 可用，否则新运行会被拒绝（可在配置中调整）。

发布包从仓库的 Releases 下载 `maestro-<版本>-<提交>.tar.gz` 与 `SHA256SUMS`，用 `sha256sum -c SHA256SUMS --ignore-missing` 核对。正式版本来自 `master` 的版本 tag；名字带 `dev-` 的预发布由 `dev` 上每次通过 CI 的推送自动生成，只用于试用，只保留最近 10 个。

先检查，不改动任何东西：

```bash
tar xf maestro-<版本>-<提交>.tar.gz && cd maestro-<版本>-<提交>
sudo ./maestroctl check --uv "$(command -v uv)"
```

每一项会给出 ok、WARN 或 FAIL，失败项附修复办法。

## 安装

```bash
sudo ./maestroctl install --port 8420 --proxy http://<代理地址>:<端口> --uv "$(command -v uv)"
# 安装时下载 Python 包也要走这个代理，就再加 --download-proxy
```

安装器依次检查主机、创建 `maestro` 账户与目录、在 `/opt/maestro/versions/` 下建好程序与 Runner 环境、写配置、生成主密钥、初始化数据库，然后注册并启动 `maestro.service`，等到服务报告就绪才结束。最后打印访问地址和**只显示一次**的初始化令牌：用浏览器打开地址，用令牌创建第一个管理员，之后的模型、凭据、数据集和成员邀请都在网页上完成。

常用选项：

| 选项 | 作用 |
| --- | --- |
| `--listen-host`、`--port` | 服务监听地址，默认 `127.0.0.1:8420`。对外访问由反向代理（nginx、Caddy 等）提供 HTTPS 和域名 |
| `--insecure-cookie` | 不经 HTTPS 直接用 HTTP 访问时需要（内网试用），否则浏览器不回传登录 cookie |
| `--proxy`、`--no-proxy` | 服务运行时的代理：写进 `/etc/maestro/environment`，供服务里的 Docker 客户端（BuildKit 取镜像仓库令牌）和 Hub 下载使用。之后直接编辑该文件再 `restart` 即可修改 |
| `--download-proxy [URL]` | 安装、更新时下载 Python 包用的代理；默认直连。只写选项不带地址，表示沿用服务的代理（`--proxy`，更新时读 `/etc/maestro/environment`）；带地址则用该地址 |
| `--docker-host` | 不用系统 Docker 时指定 Docker 地址；此时安装器不改账户的组 |
| `--uv` | uv 的路径（sudo 的 PATH 里常常没有）。安装器把它复制到 `/opt/maestro/bin/uv`，以后更新用这份 |
| `--registry-port` | 本机镜像仓库的端口，默认 8425，只绑定 127.0.0.1 |

镜像构建、Harness 安装和附件脚本经什么网络（代理、npm / pip 等包镜像源、Harness 安装时限）由管理员在网页的“管理 · 安装网络”设置，保存后立即生效，不用重启；代理必须是**容器能访问到**的地址（例如宿主机的局域网地址，不能是 127.0.0.1）。

拉取镜像走 Docker 守护进程自己的代理设置。`check` 发现守护进程没有代理时只提醒，并报告守护进程配置的 registry 镜像源，不会替你修改全机的 Docker 配置；需要时用 `sudo systemctl edit docker` 加上 `HTTP_PROXY` / `HTTPS_PROXY` 后重启 Docker。

配置项、默认值与说明见仓库的 `src/maestro/config.py` 与[运维设计](../design/12-operations.md)第 3 节。修改 `/etc/maestro/config.toml` 后执行 `sudo maestroctl restart`。旧版本写入的 `setup_proxy`、`setup_no_proxy` 已不再使用：服务会忽略它们并在日志里提醒，请在网页的“安装网络”中重新填写，再从配置文件里删去。

## 日常维护

```bash
sudo maestroctl status          # 版本、服务状态、是否就绪、运行数、磁盘
sudo maestroctl logs -f         # 服务日志（journald）；-n 200 看最近 200 行
sudo maestroctl restart         # 只启动已安装的当前版本，从不自动升级
sudo maestroctl stop | start
sudo maestroctl registry-gc     # 删除数据集版本后释放镜像仓库里不再用到的层
```

准备好的镜像存在本机镜像仓库里：同一镜像、共用的基础层只存一份，本机 Docker 里的镜像被删掉后下次用时从仓库拉回。物理删除数据集版本只删去不再被任何任务记录的镜像清单，层的空间要执行 `registry-gc` 才释放；它会让仓库暂时只读（运行照常，导入和首次准备在进行时会拒绝执行，除非加 `--force`）。

服务异常退出后由 systemd 自动重启（短时间反复崩溃会停下，等人处理）。每次启动都会先对账：上次中断时在执行的运行标为中断（已得的评分保留），按标签清理本实例遗留的容器，然后才开始受理。

## 备份与恢复

```bash
sudo maestroctl backup                    # 数据库一致快照 + 配置 + 清单
sudo maestroctl backup --include-files    # 另外复制附件、镜像仓库和产物
sudo maestroctl restore /var/backups/maestro/<时间> [--include-files]
```

- 备份写在 `/var/backups/maestro/<时间>/`，服务可以继续运行。数据库通过 SQLite 的备份接口复制，不会漏掉还在 WAL 里的写入；`manifest.json` 最后写入，没有它就说明这份备份不完整。
- **主密钥不在备份里**。清单写明数据库需要哪一代主密钥；请把 `/etc/maestro/secrets/` 另外妥善保存。恢复时如果对应的密钥不在，恢复会拒绝执行并说明原因；加 `--allow-missing-key` 可以照样恢复，但团队凭据都要重新填写。
- 恢复会停止服务、替换数据库（带 `--include-files` 时一并替换业务文件，原来的文件改名放在旁边），按需迁移数据库后再启动。备份时正在执行的运行恢复后会标为中断。
- 只支持恢复到同一台主机。

## 更新

```bash
sudo maestroctl update maestro-<新版本>.tar.gz                  # 下载 Python 包直连
sudo maestroctl update maestro-<新版本>.tar.gz --download-proxy # 下载沿用服务的代理
```

1. 在当前版本旁边装好新版本，服务继续运行；Harbor 与 Runner 没有变化时直接复用。
2. 有正在执行的运行或正在进行的导入、首次准备时拒绝更新。可以等它们结束，或加 `--interrupt-running`：这些运行会被标为中断，已得的评分保留。排队中的运行会留到新版本启动后再执行。
3. 停止服务，自动备份数据库（`/var/backups/maestro/<时间>-before-<新版本>`）。
4. 用新版本迁移数据库，切换 `current`，重建镜像仓库容器（数据不动），启动并等待就绪。
5. 迁移、启动或就绪检查中任何一步失败，都会自动换回旧版本和更新前的数据库，再启动旧版本，并以非零状态退出。新版本在失败前短暂运行期间写入的数据随之丢弃。
6. 成功后保留上一个版本供手动回退，更早的版本和不再使用的 Runner 环境会被删除。

改变准备方式的版本会让所有任务回到“待准备”，更新后管理员要在数据集页重新做首次准备；发布说明会写明。

## 卸载

```bash
sudo maestroctl uninstall                # 停止并删除服务与程序，保留配置、密钥、数据、缓存和备份
sudo maestroctl uninstall --remove-cache # 另外删除缓存
sudo maestroctl uninstall --purge        # 先列出将删除的全部内容，不删除
sudo maestroctl uninstall --purge --confirm <实例编号>
```

普通卸载之后再次 `install` 会沿用原来的配置、密钥和数据，不会发新的初始化令牌。卸载都会删除 `maestro-registry` 容器，仓库数据随 `/var/lib/maestro` 保留。`--purge` 会删除 `/opt`、`/etc`、`/var/lib`、`/var/cache`、`/var/backups` 下属于 Maestro 的目录、本实例的 Docker 容器、网络、卷与镜像（只按本实例的标签和镜像名查找），以及 `maestro` 账户。它不会清理全机的 Docker 资源，也不会动 journald。

## 从源码打包

在仓库根目录（工作树必须是干净的）：

```bash
uv sync --locked --group dev
(cd runner && uv sync --locked)
(cd frontend && npm ci)                    # 第三方许可证清单要读网页依赖的许可证
uv run python tools/build_release.py      # 写出 dist/maestro-<版本>-<提交>.tar.gz
```

包里有 `maestroctl`、服务 / Runner / SDK 的 wheel、带哈希的锁定依赖、本说明、`LICENSE`、`NOTICE`、第三方许可证清单 `THIRD_PARTY_LICENSES.md`，以及记录版本、提交、数据库结构版本、Harbor 版本和每个文件摘要的 `RELEASE.json`。网页已经编译进服务的 wheel，安装的机器不需要 git 或 Node。成员要用的 Python SDK 在包里的 `sdk/` 目录，也随每次发布推送到 Gitea 的 Python 包仓库，正式版本另由 GitHub Pages 上的索引提供，成员与 Agent 可以直接按版本安装（[SDK 说明](../../sdk/README.md)；推送令牌见[开发流程](development-workflow.md)的“SDK 包仓库”）。
