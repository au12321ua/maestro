# N-02 持续发布、CI 稳定与开源许可证（含 N-16、N-24）

状态：设计中 · 优先级：一 · 2026-10-28 · 相关：[运维](../design/12-operations.md) 第 1、2 节、[测试与验收](../design/13-testing-plan.md) 第 2、9 节、[开发流程](../guides/development-workflow.md)“发布与运行版本”、[开发交接](../HANDOFF.md) 的 CI 一条、U-29、U-31、U-37、U-40、U-42

三件事合在一份提案里，因为它们改的是同一处：CI 工作流与发布包。CD 依赖 CI 稳定，许可证清单本身又是发布包的一部分。索引里仍保留三个编号：N-24 CI 稳定、N-16 许可证、N-02 发布。

## 问题

### CI 不稳（N-24）

用户的痛点是不稳：服务器网络有问题，下载经常卡到超时；网络正常时一次 CI 只要 3～4 分钟（U-42）。Gitea 上的运行记录也是这样：

- 2026-10-24～27 的 9 次失败里，8 次失败在“Service and Runner environments”这一步（`uv sync --locked`），都是从 `files.pythonhosted.org` 下载 wheel 超时，例如 `httpx==0.28.1`，3 次重试共 44.5 秒后放弃。剩下 1 次是被新推送取消的。
- 每次运行在这一步之前，还要从 `github.com` 下载 Python（`actions/setup-python`）和 Node（`actions/setup-node`）。网络好时各要 20～30 秒；10-24 的一次运行里，这两步分别花了 23 分钟和 12 分钟。三个 action 的代码本身也每次从 GitHub 拉取。
- job 容器（`ci-base:ubuntu-24.04`）每次都是新的，所以上面这些下载一次也省不掉。镜像里的 pip 已经配了清华源，但 uv 按 `uv.lock` 里记下的地址下载，不走这个源。
- 10-28 成功的那次运行（`69d7975`）共约 4 分钟：准备环境约 1 分 45 秒，pytest 107 秒（557 个通过），`npm ci` 5 秒。

所以要解决的是下载，测试本身不慢。本机 pytest 最慢的单个测试只有 4.9 秒，时间分散在 558 个测试上，删测试省不了多少时间。

### 没有发布物（N-02）

现在的发布包要在本机手动执行 `tools/build_release.py`，再拷到服务器。U-30 定下“推送即发布，不自动部署”。本机未合入的 `feature/ci-package`（`c45704a`）把包作为 Actions 产物上传、保留 14 天，可以参考，但它不是正式发布。

### 没有许可证（N-16）

U-31 定了 Apache 2.0。旧历史里加过 `LICENSE.txt`（`55b4274`），重写后的树中已经没有了。各 `pyproject.toml` 与前端 `package.json` 都没有写许可证。

## 方案

### 1. CI 稳定（N-24）

目标：锁文件没变的推送，除了 Gitea 本身不需要连外网；网络卡住的下载要尽快失败并重试，不要等满 45 分钟。

1. **工具做进 CI 镜像**：Python 3.12、Node 24 和固定版本的 uv 直接装在 job 镜像里，工作流里去掉 `setup-python` 与 `setup-node`。镜像定义放在本仓库（`.gitea/ci/Dockerfile`），因为它的版本要和 `pyproject.toml`、`frontend/package.json` 对应；由管理员在 runner 主机上手动构建，很少重建。只剩 `actions/checkout` 一个外部 action。
2. **依赖缓存跨运行保留**：用一个 CI 专用的 Docker 命名卷放 uv 与 npm 的下载缓存，通过 act_runner 配置挂进 job 容器。缓存里只有公开的包，不放凭据；测试仍用隔离的 HOME，与主机上的真实实例互不相干。锁文件没变时，`uv sync` 与 `npm ci` 全部命中缓存。
3. **需要下载时走代理**：仓库变量 `CI_PROXY`（可以为空）设给依赖安装那几步的 `HTTPS_PROXY`，可以用部署主机已有的代理。不改 uv 的索引地址，因为那会让 `uv.lock` 在 `--locked` 下被判为过期。
4. **快速失败、自动重试**：安装依赖的步骤设单步时限（10 分钟），uv 与 npm 设较短的连接超时，整步失败后再重试两次。job 总时限从 45 分钟降到 20 分钟。
5. **小幅精简**：`test_sdk.py::test_generated_contract_and_sdk_models_are_current` 和 CI 的 `generate_clients.py --check` 查的是同一件事，CI 里只保留一处。`pytest-xdist` 并行（预计把 107 秒降到 1 分钟以内）先试：测试之间如果共享端口、文件或子进程，就不做。

不做：换成 GitHub 托管的 runner（主要测试留在 Gitea，U-42）；为了提速删测试或降低覆盖。

### 2. Gitea 上的持续发布（N-02）

在 `verify` 工作流里加一个 `release` job，依赖 `verify` 通过，并且只在本仓库的推送上运行（PR、外部 fork 和其他分支都不触发）：

| 推送 | 结果 | 版本 |
| --- | --- | --- |
| `dev` | Gitea Releases 的**预发布** | 带提交标识，例如 `0.4.0.dev0-<提交前 12 位>` |
| `master` 上的 `v*` tag | Gitea Releases 的**正式版本** | tag 必须等于包版本（`v0.4.0` 对应 `0.4.0`），不一致就失败 |
| 其他分支、`master` 分支本身 | 只跑 `verify`，不发布 | — |

- **发布内容**：`maestro-<build>.tar.gz`（已含服务、Runner 与 SDK 的 wheel）；单独的 SDK wheel，方便成员用 `pip install` 安装；第三方许可证清单（第 3 节）；`SHA256SUMS`。包本身仍由 `tools/build_release.py` 生成，不改格式。
- **dev 的版本号**：发布正式版本后，`dev` 立即把版本改成下一个 `.dev0`（例如 `0.4.0.dev0`）。现在 `dev` 上的构建名是 `0.3.0-<提交>`，看起来像 0.3.0 本身。
- **预发布的保留**：每个预发布要有一个 tag（Gitea 的发布依附于 tag），用 `dev-<提交前 12 位>`。只保留最近 10 个，更早的预发布和它们的 tag 由 job 自动删除。正式版本永不自动删除。
- **说明文字**：自动列出与上一个 tag 之间的提交；破坏性变更由发布者在 Gitea 上补写（N-17）。
- **权限**：只有 `release` job 申请 `contents: write`，用 Gitea 为 job 自动签发的令牌，不另存长期令牌。`verify` 仍是只读。
- **不自动部署**（U-29）：管理员下载包，在服务器上执行 `maestroctl update <包>`。让 `maestroctl update` 直接接受下载地址是另一件事，不在本提案内。

### 3. 开源许可证（N-16）

- 根目录放 Apache-2.0 全文 `LICENSE`，以及一个简短的 `NOTICE`（项目名、版权人、年份）。不在每个源文件头部加许可证声明。
- `pyproject.toml`（服务、Runner、SDK）写 `license = "Apache-2.0"`，用 SPDX 表达式（PEP 639）。前端 `package.json` 写 `"license": "Apache-2.0"`，`private` 保持不变。README 加一行许可证说明。
- **第三方许可证清单**由新脚本 `tools/third_party_licenses.py` 生成，发布时附上。分两部分：
  - 前端：构建产物里打包了 React 等第三方代码，属于真正的再分发，按 Apache 与 MIT 的要求附上每个包的许可证全文。
  - Python：服务与 Runner 的依赖不在包里，安装时才从 PyPI 下载，所以只列名称、版本与许可证，供使用者查看。
- **允许列表**：脚本遇到允许列表以外的许可证（GPL、AGPL、LGPL，或无法识别）时失败，CI 里也运行它。这样“发布前对当时的锁文件重新核对”就变成了自动检查。2026-10-27 的核对结果见索引中 N-16 一行。

### 4. GitHub：只发布，不重复测试（U-42）

用户只在发布 `master` 时把 `master` 和版本 tag 推到 GitHub，主要测试在 Gitea 上做。所以 GitHub 上只有一个工作流 `.github/workflows/release.yml`：

- 由 `v*` tag 触发；不跑 lint、pytest 和网页构建。
- 从同一个 tag 重新执行 `tools/build_release.py` 并生成许可证清单，然后创建 GitHub Release，资产与 Gitea 上的正式版本同名。网页已经构建好并提交在 `src/maestro/static/` 里，所以 GitHub 上不需要 Node。
- 同样核对 tag 与包版本一致。不重复测试的前提靠流程保证：Gitea 上的正式版本发布成功后才推送到 GitHub。这一条写进发布规则。
- Gitea 只读 `.gitea/workflows`，存在这个目录时会忽略 `.github/workflows`，所以两边的工作流互不触发。实施时需实测确认。

放弃的做法：由 Gitea 把已构建的包上传到 GitHub。这样两边的字节完全一致，但正好依赖不稳定的服务器到 GitHub 的网络。由 GitHub 去下载 Gitea 上的包也不行，私有 Gitea 要额外开放访问并保存令牌。两边各自构建的包内容一致（`RELEASE.json` 记的是同一个提交），只是时间戳不同；如果要求字节一致，可以按提交时间固定时间戳，见待定问题。

## 影响

- 工作流：`.gitea/workflows/verify.yml`（镜像、缓存、代理、时限、`release` job）；新增 `.gitea/ci/Dockerfile`、`.github/workflows/release.yml`。
- 代码：新增 `tools/third_party_licenses.py`；`tools/build_release.py` 加入许可证清单与 `SHA256SUMS`（如需字节一致，再加时间戳固定）；`dev` 改为 `.dev0` 版本号。
- 仓库根目录：`LICENSE`、`NOTICE`，以及 README 的许可证说明；三个 `pyproject.toml` 与 `frontend/package.json` 写明许可证。
- runner 主机：构建 CI 镜像、创建缓存卷、在 act_runner 配置中允许挂载这个卷，并设置仓库变量 `CI_PROXY`。具体步骤写进私有仓库 `maestro-site` 中 runner 主机的现场说明。
- 系统说明：[12](../design/12-operations.md) 第 1、2 节（发布物从哪里来）、[13](../design/13-testing-plan.md) 第 2、9 节、[开发流程](../guides/development-workflow.md)“发布与运行版本”、[开发交接](../HANDOFF.md) 的 CI 一条、[部署指南](../guides/deployment.md)（从 Releases 下载）。

## 验收

- 锁文件没变时，即使 runner 连不上 `github.com` 与 `files.pythonhosted.org`（把代理指向一个不存在的地址来模拟），CI 仍然通过。
- 人为让依赖下载卡住时，这一步在时限内失败并自动重试，整个 job 不会跑满时限。
- 推送到 `dev` 并通过后，Gitea Releases 出现带提交标识的预发布，内含包、SDK wheel、许可证清单和 `SHA256SUMS`；超过 10 个时最早的一个被删除。用这个包执行安装测试（`MAESTRO_INSTALL_TESTS=1`），`maestroctl update` 能完成更新。
- 在 `master` 上打与包版本一致的 tag，产生正式版本；版本不一致时 job 失败，不产生发布。
- 在测试用的 GitHub 仓库或 fork 上推送 tag，产生 GitHub Release，且没有运行任何测试。
- 给许可证脚本一个含 GPL 依赖的样例时，它失败。

## 待定问题

1. CI 镜像定义放本仓库（推荐，版本与锁文件一起改），还是放 `maestro-site`？
2. 依赖缓存用 CI 专用的命名卷（推荐，简单，不依赖 act_runner 的缓存服务），还是用 `actions/cache`？
3. 正式版本的 tag 推送要不要再完整跑一次 `verify`？推荐再跑一次：多约 4 分钟，但能保证发布物来自刚测过的同一份代码，不必去查之前那次运行的状态。
4. GitHub 上由 tag 重新构建（推荐），还是要求两边的包字节完全一致（需要固定时间戳，多一点工作量）？
5. `NOTICE` 里的版权人写 `auua`，还是写 “The Maestro Authors”？后者便于以后接受外部贡献。
6. 是否用工作流的 `paths-ignore` 自动跳过只改 Markdown 的推送，代替手写 `[skip ci]`（U-37）？推荐采用：判断规则相同，只是不再靠人记。
7. `pytest-xdist` 先试再决定（推荐），还是这次不碰？
