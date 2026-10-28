# N-24 CI 稳定：剩下的主机一侧

状态：实施中 · 优先级：一 · 2026-10-28 · 相关：[开发交接](../HANDOFF.md) 的 CI 一条、[测试与验收](../design/13-testing-plan.md) 第 2 节、U-37、U-42、U-43

N-02（持续发布）与 N-16（许可证）已经完成。N-24 中不依赖主机的部分也已合入：依赖下载的单步时限、短超时与整步重试，可选的下载代理 `CI_PROXY`，去掉与 `test_sdk.py` 重复的契约检查，pytest 用 `-n 4` 并行，以及用 `paths` 自动跳过只改 Markdown 的推送。本提案只剩下要在 runner 主机上做的部分。按 U-43，这部分由用户在主机上另找 Agent 实施，这里只写思路。

## 问题

2026-10-24～27 的 9 次 CI 失败里，8 次是 `uv sync` 从 `files.pythonhosted.org` 下载 wheel 超时。每次运行还要从 `github.com` 下载 Python 与 Node（`setup-python`、`setup-node`）：网络好时各 20～30 秒，10-24 那次分别花了 23 分钟和 12 分钟。job 容器 `ci-base:ubuntu-24.04` 每次都是新的，这些下载一次也省不掉。成功的一次（10-28，`69d7975`）约 4 分钟，其中 pytest 107 秒。镜像里的 pip 已配清华源，但 uv 按 `uv.lock` 里的地址下载，不走这个源。

## 方案（思路）

1. **工具做进 CI 镜像**：在 `maestro-site` 里写 job 镜像的定义，装好 Python 3.12、Node 24 与 uv 0.13.5（版本以本仓库工作流与 `pyproject.toml`、`frontend/package.json` 为准），在 runner 主机上构建。之后本仓库的工作流用仓库变量引用这个镜像，去掉 `setup-python` 与 `setup-node`，并在开头核对镜像里的版本，对不上就失败并提示重建镜像。
2. **依赖缓存跨运行保留**：建一个 CI 专用的 Docker 命名卷，在 act_runner 配置里允许并挂进 job 容器，作为 uv 与 npm 的下载缓存（工作流里设 `UV_CACHE_DIR`、`npm_config_cache` 指向它）。卷里只有公开的包，不放凭据，测试仍用隔离的 HOME。
3. **Actions artifacts 上传失败**：2026-10-28 两次运行（134 及其重跑）中，`actions/upload-artifact@v4` 都在 `CreateArtifact` 时 ECONNRESET，而同一容器从 Gitea 检出代码正常。runner 是 `gitea-runner-wsl v3.4.0`。查明 job 容器里 artifact 接口的地址（Gitea 的 `ROOT_URL` / `LOCAL_ROOT_URL`、代理设置）为什么连不通。修好之前发布在同一个 job 里完成，不依赖 artifact；U-35 约定的“CI 证据用 Actions artifacts”同样受影响。
4. **代理**：如主机有可用代理，设仓库变量 `CI_PROXY`。工作流已经支持，只用在下载依赖与打包的步骤上。
5. **收尾**：主机一侧就绪后，本仓库把 job 总时限从 45 分钟降到 20 分钟，把系统说明里 CI 的描述改成现状，删除本提案。

主机一侧的步骤与结果记入 `maestro-site` 中 runner 主机的现场说明。

## 验收

- 锁文件没变时，即使 runner 连不上 `github.com` 与 `files.pythonhosted.org`（例如把 `CI_PROXY` 指向一个不存在的地址），CI 仍然通过。
- 人为让依赖下载卡住时，这一步在时限内失败并重试，整个 job 不跑满时限（本仓库的部分已经做了，随主机一侧一起实测）。
