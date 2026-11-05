# N-30 问题记录与测试目录

状态：实施中（问题记录已并入[开发与协作](../guides/development-workflow.md)“问题与修复”，测试目录待实施） · 优先级：一 · 2026-11-04 · 相关：[开发验证](../guides/testing.md)、[13](../design/13-testing-plan.md) 第 2 节、U-57、U-65、U-70～U-72

## 问题

1. **问题没有固定的去处。** 当场修不了、要调查或跨会话跟进的问题，现在只散在对话、HANDOFF 或现场报告里，修复后很难从代码追回原因。
2. **测试按历史而不是按行为组织。** `tests/maestro/` 平铺了 65 个文件（约 1.7 万行、796 项默认测试，`-n 4` 约 70 秒）。其中 18 个 `test_fix_*` 是两轮审查修复留下的，与同一功能的其他测试分在不同文件；需要真实 Docker / Hub / 安装的测试与默认测试同目录；`test_acceptance*.py` 实际测的是准入事务与竞争，与 13 的 E2E 故事不对应。修问题要补回归测试时，不知道放在哪里，按问题再加文件只会更乱。

速度目前不是问题，本提案只解决“放在哪里、叫什么”。

## 方案

### 问题记录

- **位置**：主仓库 `PhySpace/maestro` 的 Gitea issues（已定）。不另建编号体系，也不放 `maestro-site`。
- **何时建**：只在当场修不了、需要调查或要跨会话跟进时建 issue；能当场修的直接开 `fix/` 分支，不建 issue。
- **状态**：open / closed，不修的加 `wontfix` 标签后关闭。是否在修，看有没有对应分支。
- **引用写明平台**（已定）：写作 `Gitea#12`，避免主分支推到 GitHub 后 `#12` 被链接到 GitHub 的同号 issue。分支 `fix/<编号>-<短名>`，提交与 PR 写 `Fixes Gitea#12`。带平台前缀不会触发 Gitea 的关键词自动关闭，合入 `dev` 后手动关闭。
- **公开与私有分开**：issue 是公开的，只写现象、原因与能用 fixture 复现的步骤；现场主机信息、原始日志与 capture 仍放 `maestro-site`（U-57），issue 里注明证据路径（公开说明不链接私有档案）。
- **外部贡献者、GitHub issues 的分流**：等有外部用户时再考虑（已定）。

### 代码注释可以引用 issue（放宽 U-65）

issue 编号分配后不变、关闭后仍可访问，不像文档章节与提案会重排或归档，因此代码注释与测试说明可以引用 `Gitea#N`（已定）。前提是注释先写出约束本身，引用只作补充，例如“必须先关外键再批量迁移，否则 SQLite 会删掉被引用的 PAT（Gitea#14）”。文档路径、章节与 U / N / 验收编号仍按 U-65 不进代码。

### 修复收尾自查

写入[开发与协作](../guides/development-workflow.md)：

1. 加了能复现该问题的回归测试吗？放进所测行为所在的文件，按行为命名，不按 issue 命名。
2. 修法不显然时，修复处有注释写明约束吗？
3. 揭示了没记录的约束吗？有的话把约束本身写进系统说明，不写问题经过。
4. 发现是设计本身的问题吗？是的话开提案，必要时请用户决定。
5. 是外部或环境问题吗？绕行办法写进[开发验证](../guides/testing.md)“环境陷阱”或[部署指南](../guides/deployment.md)，不另建“已知问题”页。

多数修复只需要第 1 步和提交说明。

### 测试目录

一次性分好（已定），只移动、拆分、合并与改名，不改测试逻辑：

| 目录 | 内容（现有文件，草案） |
| --- | --- |
| `identity/` | `identity_api`、`permissions`、`security`、`credential_visibility`、`fix_identity_*`（除 `cli`、`docker`） |
| `datasets/` | `datasets`、`preparation`、`preparation_images`、`fix_prep_*` |
| `configuration/` | `configuration`、`plans`、`harness_upstream`、`setup_network`、`attachments` |
| `runs/` | `acceptance`、`acceptance_p0`（改为按准入事务与竞争命名）、`batches`、`capacity`、`reruns`、`worker`、`restart`、`execution_policy`、`harbor_executor`、`harbor_boundary`、`network_capability`、`limits_wiring`、`fix_m5_review`（按内容拆入各目录） |
| `results/` | `scoring`、`fix_scoring_*`、`comparison`、`collections`、`results_export`、`artifact_archive`、`retention`、`recycle`、`fix_artifacts_*`（除 `runner`） |
| `runner/` | `runner_attachments`、`runner_container_harnesses`、`runner_network`、`fix_artifacts_runner` |
| `ops/` | `config`、`cli`、`migrations`、`backup`、`maestroctl`、`release_tools`、`fix_identity_cli`、`fix_identity_docker` |
| `contract/` | `api_contract`、`sdk`（契约与生成物核对部分） |
| `e2e/` | `e2e_run`、`sdk` 中的三个故事，对应 13 的 E2E 场景 |
| `live/` | `harbor_docker`（按 Oracle / Terminus-2 / 真实模型拆分）、`hub_live`、`install` |

- `test_fix_*` 并入同一行为的文件，或按行为改名（如登录限流 `test_login_throttling.py`）；说明里的“Review fix:”改写为行为本身。顺带清掉测试说明里残留的文档章节引用（U-65）。
- `live/` 只靠目录区分，沿用现有 `MAESTRO_*_TESTS` 开关，不加 pytest marker。
- `conftest.py`、`support.py`、`fake_runner.py`、`fixtures/` 留在 `tests/maestro/`；用 pytest 的 `pythonpath` 与 `importlib` 导入模式解决子目录导入与同名文件。

放弃的做法：按单元 / 集成 / 验收分层（大部分测试同时经过服务与数据库，层次划不清，修问题时也不能帮你找到文件）；分批搬迁（中途新旧布局并存，更难判断新测试放哪）。

## 影响

- 文档：[开发与协作](../guides/development-workflow.md) 加问题记录与收尾自查；[开发验证](../guides/testing.md)、[13](../design/13-testing-plan.md) 第 2 节、[源码职责](../architecture/source-layout.md)、HANDOFF、09 / 10 / 12 / 15 中的测试路径；`AGENTS.md` 中 U-65 的表述。冻结的 `records/` 不改。
- 决策记录：登记问题记录位置与引用格式、放宽 U-65 两条用户决定。
- 代码：`pyproject.toml` 的 pytest 配置；CI 的 ruff 路径不变（仍为 `tests/maestro`）。无接口、数据结构与部署变化。

## 验收

- 重组前后默认测试数量一致（当前 796 通过、39 跳过），`ruff check` / `format --check` 通过，CI 绿。
- 真实 Docker、Hub、安装测试在新路径下仍能按原开关收集（`--collect-only` 核对数量一致）。
- 文档中不再有指向旧测试路径的引用（冻结记录除外），`check_doc_links.py` 通过。
- 问题记录：仓库有 `wontfix` 标签（已建），流程写入[开发与协作](../guides/development-workflow.md)。

## 待定问题

- 上面的目录与归属是草案，实施时按文件内容微调。
- 已定：问题记录与测试重组分两个 PR，前者只改文档，后者改代码与路径。

## 结果

（归档时填写。）
