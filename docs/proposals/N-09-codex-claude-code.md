# N-09 常规 Harness：Codex 与 Claude Code，以及上游版本提示

状态：已确认 · 优先级：二 · 2026-10-28 · 相关：[配置与附件](../design/03-configuration-attachments.md) 第 2、7 节、[网络与权限](../design/09-identity-security.md) 第 5 节、[运维](../design/12-operations.md) 第 3 节、[Harbor 集成](../design/15-stack-harbor.md) 第 5、8 节、U-25、U-30、U-38、U-40、U-41

## 问题

U-30 定下 Harness 至少支持 Codex 与 Claude Code。现在只有两个适配器：Terminus-2 在 Runner 里调模型，OpenCode 装在任务容器里、从容器调模型。团队实际想比较的常用 Harness 还用不了。

另一个问题出在版本上。管理员发布 Harness 版本时填的“版本”只是 Maestro 里的标签，真正装进容器的 OpenCode 版本藏在“参数定义”JSON 的 `version` 默认值里；没写就用代码里固定的 `1.18.35`，而上游已经到 `1.19.4`。成员还能在单个运行里改 `version` 参数。所以管理员从表单上看不出实际装的是哪个版本，更不知道上游最新是多少。这两个 Harness 迭代得很快（2026-10-28 从 npm 查到）：

| Harness | npm 包 | 上游标签 | 备注 |
| --- | --- | --- | --- |
| Claude Code | `@anthropic-ai/claude-code` | stable `2.1.308`、latest `2.1.311`、next `2.1.312` | 10-20～10-27 八天发了 8 个版本；官方下载站的 latest 是 `2.1.312`，与 npm 的 latest 不同 |
| Codex | `@openai/codex` | latest `0.165.0` | 版本号超过 5600 个，绝大多数是 alpha 和按平台拆的包 |
| OpenCode | `opencode-ai` | latest `1.19.4` | Maestro 默认 `1.18.35` |

## 方案

### 1. 两个新适配器

以锁定的 Harbor 0.24.0 自带的 `codex`、`claude-code` agent 为基础，像 OpenCode 一样在 Runner 里写平台子类。先把 OpenCode 子类里通用的部分抽成“容器内 Harness”基类，三个适配器共用：

- 模型密钥只在 Agent 运行期间以权限 400 的文件放进容器，验证前删除；不经命令行或环境变量（`docker exec -e` 的值在本机进程列表里人人可见）。
- 不读 Runner 自身的环境变量；setup 使用部署的安装网络，Agent 阶段只能访问模型地址所在主机。
- 镜像里已有同一版本就跳过安装；附件在 Harness 自身安装之前装好。

两者各自的差异：

| | Codex | Claude Code |
| --- | --- | --- |
| 可调用的协议 | 只有 `openai`（Responses API）；选 `openai-compatible` 或 `anthropic` 模型时在解析阶段拒绝 | 只有 `anthropic`；其他协议在解析阶段拒绝 |
| Harbor 默认怎么给密钥 | 环境变量 `OPENAI_API_KEY`，再用它生成 `auth.json` | 环境变量 `ANTHROPIC_API_KEY` |
| Maestro 改为 | 直接把 `auth.json` 作为 400 文件上传到 Codex 的密钥目录 | 在 `settings.json` 里设 `apiKeyHelper`，由它读取密钥文件 |
| 模型地址 | 写进 `config.toml` 的 `openai_base_url`（Harbor 注明 Codex 不认环境变量） | 环境变量 `ANTHROPIC_BASE_URL`（不是秘密） |
| 安装 | npm；Harbor 的安装命令写死 Node 下载地址，安装网络里的同名变量优先 | Harbor 在 glibc 镜像上用 curl 从官方下载站装，只能靠代理；musl 镜像走 npm |
| 其他 | 关掉网页搜索；容器本身就是沙箱，沿用 Harbor 的“跳过审批与沙箱”启动方式 | 关掉网页工具；关掉自动更新与非必要的外联，免得在收紧的网络里卡住 |

执行页像 OpenCode 一样实时显示可读的 Agent 日志：Runner 读两者的 JSON 事件流（Codex 的 `codex.txt`、Claude Code 的 stream-json），转成文字作为 log 事件；用量取 Harbor 汇总的 token 数。

只支持 API 密钥，不支持 ChatGPT 或 Claude 订阅账号登录；不用 Harbor 的 ACP 模式。

### 2. 上游版本成为 Harness 版本的一部分

Harness 版本新增必填字段“上游版本”，即装进容器的 CLI 版本号：

- 只接受确切的版本号，不接受 `latest`、标签或范围，保证重跑装的是同一个版本；它计入 Harness 版本的内容摘要，发布后不能修改。
- 从可暴露的参数里去掉 `version`，成员不能在单个运行里换版本；换版本就是管理员发布一个新的 Harness 版本。
- “版本”一栏仍是 Maestro 里的名字，表单默认填成与上游版本相同。
- 已有的 OpenCode Harness 版本由追加的迁移补上上游版本：参数定义里有 `version` 默认值就用它，否则用 `1.18.35`。

### 3. 发布表单上的版本提示

管理员在管理页“Harness”选好适配器后，上游版本输入框下面列出可选版本，点一下即填入：

~~~text
执行器适配器   [ claude-code ▾ ]
上游版本       [ 2.1.308          ]
  上游（npm 镜像，10-28 14:02 查询）
    stable  2.1.308   10-22 发布   [填入]
    latest  2.1.311   10-26 发布   [填入]   ⚠ 比 Maestro 测过的版本新
  Maestro 测过    2.1.30x   本发布实测通过         [填入]
  团队已发布      2.1.290   claude-code-2.1.290，启用中
  [展开最近 10 个正式版本]
~~~

- **数据来源**是该适配器 npm 包的元数据（标签和每个版本的发布时间），由服务端查询，走安装网络里的代理和 npm 镜像源（安装变量 `npm_config_registry`），没有设置就用 npm 官方地址。安装能通的网络，查询也能通。三个 Harness 都在 npm 上发布同一套版本号，所以 Claude Code 虽然用 curl 安装，也查 npm。
- **只列正式版本**：过滤掉 alpha、beta、按平台拆分的包和快照。Claude Code 显示 `stable` 与 `latest` 两个标签，Codex 与 OpenCode 显示 `latest`。
- **Maestro 测过的版本**：每个适配器在代码里记一个经真实模型冒烟通过的版本，随发布更新。填的版本比它新时显示黄色提醒“这个版本未经当前 Maestro 实测，建议先跑一个小运行”，不阻止发布：上游 CLI 改了参数或日志格式，锁定 Harbor 里的适配代码可能对不上。
- **团队已发布**：列出已有的同适配器 Harness 版本，方便看出差了多少。Harness 列表里每个版本旁也标出“上游已有 x.y.z”。
- **查询时机**：打开表单时查一次，旁边有刷新按钮；服务端缓存 10 分钟。查不到（离线、镜像不可达）时显示原因，手填照常可用。
- **发布时核对**：查得到元数据时，版本号不存在就拒绝发布（拼错的版本否则要到第一个试次的 setup 才失败）；查不到时只提醒。

不做：自动跟随最新版本（破坏可复现）、后台定时检查和通知、发布前在临时容器里试装（同 N-08 的“保存前试一下”，以后再说）。

### 4. 本提案不做的事

- **声明式 Harness 配置**（N-19）：先用代码写这三个适配器，看清通用部分后再设计声明式格式。
- **升级 Harbor**：Harbor 0.25.0 已于 2026-10-25 发布。先在 0.24.0 上实测；升级 Harbor 单独做，届时连同 [15](../design/15-stack-harbor.md) 第 11 节的 schema 导出、`--dry-run` 与兼容清单一起补。风险是 0.24.0 的适配代码针对较旧的 CLI 写成（注释里提到 Codex 0.118.0），最新 CLI 可能对不上；那样就把“测过的版本”定在能用的版本，并在 PR 中说明。
- **与独立验证环境（N-01）同时开发**：两者都改 Runner 的适配器与网络。

### 5. 实测预算

真实模型实测合计不超过 20 美元（U-40），开销写进 PR 描述。

## 影响

- 系统说明：[03](../design/03-configuration-attachments.md) 第 2 节（适配器列表、上游版本字段）与第 7 节；[09](../design/09-identity-security.md) 第 5 节（容器内调模型的密钥文件）；[15](../design/15-stack-harbor.md) 第 5 节凭据一段与第 8 节新增的私有接口复核项；[12](../design/12-operations.md) 第 3 节最后一条；[11](../design/11-web-ui.md) 管理页 Harness；[13](../design/13-testing-plan.md) 真实模型冒烟一行。
- 数据：`harness_versions` 加上游版本列，追加迁移（U-34）。
- 接口：发布 Harness 版本的请求与返回带上游版本；新增管理员网页会话专用的上游版本查询接口；重新生成 OpenAPI、SDK 模型与网页类型。
- 代码：`runner/src/maestro_runner/agents.py`、`job.py`（日志渲染），`src/maestro/execution/harbor.py` 的适配器清单，管理页 Harness 表单。

## 验收

- Codex（Responses 协议的模型）与 Claude Code（Anthropic 协议的模型）各完成至少一个真实模型试次，有评分、实时日志与用量；Agent 运行期间本机进程列表里看不到密钥，验证开始前容器里的密钥文件已删除，Agent 阶段只能访问模型主机。
- 协议不匹配（例如 Codex 配 Anthropic 模型）在解析阶段报 `HARNESS_PROTOCOL_UNSUPPORTED`，不启动容器。
- 镜像里已有同一版本时跳过安装；只有代理、或只有 npm 镜像源时，各自能装上的情况与 [12](../design/12-operations.md) 第 3 节的说明一致。
- 上游版本：`latest` 和不存在的版本号被拒绝；成员不能在运行里改版本；已有 OpenCode Harness 迁移后装的版本不变。
- 版本提示：能列出正式版本与发布时间并一键填入；比测过的版本新时有提醒；npm 不可达时表单仍可手填发布。
- 真实模型开销不超过 20 美元。

## 已定问题

2026-10-28 用户确认全部按推荐（U-41）：

1. 成员不能在单个运行里换上游版本，换版本由管理员发布新的 Harness 版本。
2. Claude Code 先实测 npm 包在同一版本下能否正常工作，能就统一走 npm 安装（可用镜像源）。
3. Claude Code 的版本提示默认推荐 `stable`，`latest` 并列显示。
4. 只支持 API 密钥，不支持订阅账号登录。

仍需实测确认：Codex 直接读上传的 `auth.json`、Claude Code 的 `apiKeyHelper` 在中转站上都能用（有的中转要求 `Authorization: Bearer`）；最新 CLI 与 Harbor 0.24.0 的适配代码是否兼容。
