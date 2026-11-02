# 系统架构

导航：[文档入口](../README.md) · [系统总览](../design/00-overview.md) · [源码职责](source-layout.md)

本页描述当前系统的进程、边界与依赖方向；模块行为和取舍由[系统说明](../design/README.md)维护。

## 结构与依赖方向

~~~mermaid
flowchart TB
    Web[网页] --> API[统一 API 与身份权限]
    SDK[Python SDK] --> API
    API --> Catalog[数据集与配置目录]
    API --> Runs[方案、提交与运行]
    API --> Results[结果、产物与导出]
    Runs --> Queue[持久队列与调度]
    Queue --> Worker[进程内 Worker]
    Worker --> Executor[HarborExecutor]
    Executor --> Runner[Runner 子进程 / 锁定 Harbor]
    Runner --> Docker[本机 Docker]
    Catalog --> Source[HubSource，经 Runner]
    Catalog --> Env[DockerEnvironment 准备]
    Env --> Docker
    Worker --> Store[ArtifactStore / 本地文件]
    Runs --> DB[(SQLite WAL)]
    Results --> DB
    Catalog --> DB
~~~

业务层使用自己的 DatasetVersion、TaskAdmission、RunSnapshot、TrialResult 等对象；Hub 响应在数据源适配器中转换，Harbor 的配置与结果类型只出现在 Runner 内，服务进程不 import harbor。外部边界是 `src/maestro/boundaries/` 中的四个协议：DatasetSource（`HubSource`）、Executor（`HarborExecutor`，含按标签清理与重启对账）、Environment（`DockerEnvironment`，服务进程直接调用 Docker 准备与恢复镜像）、ArtifactStore（`LocalArtifactStore`）；测试替身在 `src/maestro/testing/fakes.py`。管理员首次准备与运行内恢复调用同一准备接口，权限入口与准入处理分开。

## 从实现进入

- 服务装配：[Maestro.build](../../src/maestro/application.py)。业务层使用平台自己的类型，四个边界定义在 [boundaries](../../src/maestro/boundaries/)。
- 执行链路：[Worker](../../src/maestro/services/worker.py) 调用 [HarborExecutor](../../src/maestro/execution/harbor.py)，通过独立进程运行 [Runner](../../runner/src/maestro_runner/job.py)。
- 契约与客户端：[OpenAPI 导出](../../src/maestro/api/openapi.py)与[生成工具](../../tools/generate_clients.py)保持服务、SDK 和网页类型同源。
- 完整目录分工、安装与发布工具见[源码职责](source-layout.md)。
