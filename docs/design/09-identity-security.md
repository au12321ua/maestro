# 09 身份、权限与安全边界

导航：[总览](00-overview.md) · 依赖：[提交](04-plans-submission.md)、[运维](12-operations.md) · 相关代码：`src/maestro/security/`、`src/maestro/services/{accounts,pats,principal,credentials}.py`、`src/maestro/api/deps.py`、`runner/src/maestro_runner/network.py`

## 1. 身份模型

单实例单团队，只有管理员和成员，不开放注册，不接 SSO。结果团队共享，提交保留个人身份。首个管理员用服务生成的一次性初始化令牌创建，之后入口永久关闭；其他人凭管理员签发的单次、有期限的邀请链接加入，重置密码也是一条邀请链接。密码用 Argon2id（参数随哈希保存，登录时按需重哈希）；会话、PAT、邀请令牌都是带类型前缀的随机值，库里只存摘要。

| 编号 | 要求 |
| --- | --- |
| IAM-01 | 会话与 PAT 明确区分，所有业务授权在服务端执行 |
| IAM-02 | PAT 只有 read 与 experiment 两类，永不具备管理员能力 |
| IAM-03 | 写操作和登录结果写审计；未满足：被拒绝的授权（403）不写审计 |
| IAM-04 | 凭据明文不返回，不进入快照、导出或普通日志 |
| IAM-05 | 执行容器按阶段限制网络；未满足：PID 数与磁盘用量不由平台限制 |
| IAM-06 | 账户停用、PAT 撤销 / 过期在下一次请求即生效 |

## 2. PAT 规则

有效权限 = 用户角色 ∩ PAT scope ∩ 动作 ∩ 对象归属（`services/principal.py`）；管理员的 PAT 也只有成员级能力。

- 只能由本人在网页会话中创建、查看、撤销；原始值只返回一次。PAT 不能管理 PAT。
- read 读取团队业务数据并可发起本人导出；experiment 另可管理本人方案、附件、运行和批次。
- 必有 expires_at 与 run_limit（缺省与上限来自团队策略）；accepted_runs 只在受理事务内原子增加，批次按运行数扣减。
- 撤销、到期或用户停用只阻止后续调用，不终止已受理的运行。

已知限制：没有续期或增额，只能新建 PAT。

## 3. 权限矩阵

“本人”按资源 owner_id 判断，不能由请求参数指定。管理员的覆盖能力是取消、停用、归档和删除，不包括改写他人方案。

| 对象 / 动作 | 成员网页 | 管理员网页 | read PAT | experiment PAT |
| --- | --- | --- | --- | --- |
| 读取目录（脱敏）、附件、运行、结果、产物、批次、方案、凭据名称（U-23） | 允许 | 允许 | 允许 | 允许 |
| 发起导出，查看与下载导出包 | 本人的 | 含他人的 | 本人的 | 本人的 |
| 创建 / 复制方案，上传附件，提交、重跑、修改后运行（新运行归调用者，U-08） | 允许 | 允许 | 禁止 | 允许 |
| 修改方案内容 | 本人的 | 本人的 | 禁止 | 本人的 |
| 归档方案，启停附件，取消运行 / 批次，已结束对象放进回收站（U-19） | 本人的 | 含他人的 | 禁止 | 本人的 |
| 创建 / 撤销 PAT | 本人的 | 本人的 | 禁止 | 禁止 |
| 删除 PAT（进回收站） | 本人的 | 含他人的 | 禁止 | 禁止 |
| 数据集导入与 Hub 检索（U-20）、首次准备及重试、撤销准入、回收缓存、评分契约 | 禁止 | 允许 | 禁止 | 禁止 |
| 模型、Harness、团队策略、容量、安装网络、凭据与主密钥管理；查看部署配置与模型绑定的凭据（U-27） | 禁止 | 允许 | 禁止 | 禁止 |
| 成员、邀请、重置密码链接；删除数据集 / 模型 / Harness / 凭据 / 产物 / 账户；回收站与审计 | 禁止 | 允许 | 禁止 | 禁止 |
| 读取凭据明文；把模型 API 密钥用作任务秘密参数（U-28） | 禁止 | 禁止 | 禁止 | 禁止 |

删除账户时不能删自己、最后一个管理员或仍有排队 / 执行中运行的账户。更新、备份、恢复走主机上的 `maestroctl`，不经 HTTP（[12](12-operations.md)）。集合与分析快照的权限见 [N-11](../proposals/N-11-collections-comparison.md)。

管理员能力要求 role=admin 且通过网页会话，PAT 一律得到 ADMIN_SESSION_REQUIRED。`/api/v1/admin/` 路由与 Hub 检索在路由层挂 `admin_session` 守卫，`tests/maestro/test_permissions.py` 遍历路由核对；`DELETE /artifacts/{id}` 等混合权限接口在服务内按对象检查，服务方法也都自行再查一次。

缓存恢复和附件 bootstrap 属于已受理运行，由系统按固定规格执行，成员和 PAT 没有单独触发的入口；“仅管理员”只限首次准备。共享恢复 Operation 不属于任何等待者，等待者只能取消自己的运行。导出 Operation 只对本人和管理员可见。

## 4. 会话、凭据与审计

- **会话**：HttpOnly、SameSite=Lax cookie，Secure 由 `cookie_secure` 配置，TLS 由反向代理终止；写请求须带由会话令牌 HMAC 派生的 `X-CSRF-Token`。登录、初始化、邀请和改密码按来源与账户限速。
- **凭据**：AES-256-GCM，随机 nonce，附加认证数据为凭据 id 与修订，防止密文被换到另一行。配置和快照只存引用，Worker 执行时解密注入；没有 API 返回值或值的摘要。
- **主密钥**：按代次存为数据目录之外的独立文件，库里只存代次与校验值；轮换在一个事务中重加密全部凭据，旧文件保留。主密钥不可用时不受理新运行。备份含凭据密文，不含主密钥。
- **审计**：`audit_events` 记录登录（含失败）、账户与 PAT、提交与取消、配置与策略、数据集准入、凭据与主密钥、附件和回收站操作；不含秘密值，不自动清理。

已知限制：运行不记录执行时实际使用的凭据修订号。

## 5. 容器与阶段网络

团队受邀且可信，网络限制的目的是让模型运行只能访问所选模型服务，不是对抗恶意任务。平台复用 Harbor 的分阶段网络与 egress-control sidecar（nftables），由 Runner 收紧（U-18、D-015）：

| 阶段 | 网络 |
| --- | --- |
| 环境基线 / agent.setup | 任务作者的设置（安装依赖、附件 bootstrap 需要外网） |
| agent.run | allowlist，只含模型 endpoint 主机；Harness 在 Runner 侧调模型时（如 terminus-2）为 no-network |
| verify | no-network |

- 只收紧模型运行；Oracle 是测试模式，按作者设置执行（`execution/harbor.py` 的 `_network`）。
- 每阶段取作者与平台中更严的一方，allowlist 取交集。Harbor 的 `extra_allowed_hosts` 只能放宽，所以 Runner 改写任务私有副本的 `task.toml`，缓存原件不变，生效计划记为 `network_plan` 事件。
- Runner 包装 Harbor 的策略切换：受限后不再回到 public（Harbor 会在阶段之间重新放开），并重置未经出口代理的 TCP 连接。
- 容器内调模型的 Harness（OpenCode、Codex、Claude Code）只在 Agent 运行期间拿到密钥：Runner 用文件传输把它放进容器，权限 400、属主为任务的 Agent 用户，结束后、验证前删除；不经命令行或环境变量（`docker exec -e` 的值本机进程列表人人可见）。OpenCode 经配置读密钥文件，Claude Code 经 `apiKeyHelper` 读，Codex 拿到复制进去的 `auth.json`。
- 不降级：作者 compose 中自带网络的服务会绕过 sidecar，运行以 NETWORK_POLICY_UNSUPPORTED 拒绝；主机不支持 nftables 时配置解析即给出同名 blocker，Runner 启动前再查一次。

容器不挂载 Docker socket、数据库、主密钥或宿主目录，唯一例外是本试次的 agent / verifier / artifacts 输出目录（U-16）；附件与任务材料复制进容器。附件 bootstrap 在所属试次容器内各自安装，失败不影响其他试次或准入。平台下发 CPU 与内存限制，不用 privileged。日志与产物经 `security/redaction.py` 基本脱敏（已知秘密值、Maestro 令牌、认证头与键值、提供商 key 形态、URL 密码）。

已知限制：

- Harness 与任务代码同容器，任务代码可能读到模型凭据（可信团队假设下接受）。
- 脱敏识别不了编码或变形后的秘密。
- DNS、ICMP 等非 TCP 出口未验证完全封闭；模型服务端的 web 工具、远程 MCP 不受容器网络控制。
- PID 数与磁盘用量不由平台限制。

## 6. RSI 扩展边界

不提供。read PAT 能读完整业务证据，不隐藏参考答案或验证代码。

## 7. 验收

- 管理员的 PAT 调用管理接口被拒绝，管理员网页会话可以执行。
- 成员可读他人结果但不能取消他人运行；篡改 owner_id 无效。
- 撤销 PAT 或停用账户后下一次调用失败，已受理运行继续。
- experiment PAT 提交的运行可自动恢复缓存和执行 bootstrap；read PAT 不能触发，等待者不能取消共享恢复。
- 快照、导出、错误、API 响应和普通日志不出现已知凭据明文。
- 模型运行的执行 / 验证阶段不能访问任意外网与宿主管理面，仍能调用所选模型；主机无 nftables 支持时明确拒绝。
