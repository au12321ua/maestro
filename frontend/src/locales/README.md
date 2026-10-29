# Web UI languages

`zh-CN/` and `en/` hold every text the web UI shows, one JSON file per area (`common`, `runs`,
`datasets`, …); `src/i18n.ts` loads them all. Components call `t("<file>.<key>")` imported from
`@/i18n`, or `<Trans>` for a sentence with markup. Keys are semantic camelCase names grouped by
component or section; the Chinese text is the source. `npm run check:i18n` (also in CI) fails when
the languages differ in keys, `{{variables}}` or `<tags>`, or when Chinese text is written in code.

- Interpolation: `{{name}}`. Plurals: English uses `key_one` / `key_other` with `t(key, { count })`;
  Chinese has a single `key`.
- Label maps keyed by an API code (states, reasons) are objects read with `{ returnObjects: true }`.
- Error codes from the API are translated in `errors.json` (`errors.codes.<CODE>`); a code without
  text shows the server's English message.
- The English text is AI-translated and not reviewed by a human (U-58); the language switch says so.

## Terms

Keep names that the API, SDK and Harbor already use in English.

| 中文 | English |
| --- | --- |
| 运行 | run |
| 批次 | batch |
| 任务 | task |
| 试次 | trial |
| 执行尝试 | attempt |
| 数据集 / 版本 | dataset / version |
| 准备 / 首次准备 | preparation / first preparation |
| 准入 | admission (admitted) |
| 缓存 | cache |
| 可提交 | submittable |
| 作者参数 | author parameters |
| 运行配置 | run configuration |
| 模型配置 | model configuration |
| 凭据 | credential |
| 提供商 | provider |
| 附件 / 快照 | attachment / snapshot |
| 实验方案（方案） | experiment plan (plan) |
| 集合 | collection |
| 分析快照 | analysis snapshot |
| 比较 | comparison / compare |
| 产物 | artifact |
| 导出 | export |
| 回收站 / 物理删除 | recycle bin / permanently delete |
| 评分 / 分数 / 有效零分 | scoring / score / valid zero |
| 验证器 | verifier |
| 容量 | capacity |
| 排队 / 受理 | queue / accept |
| 成员 / 管理员 | member / admin |
| 访问令牌（PAT） | access token (PAT) |
| 邀请 | invitation |
| 主密钥 | master key |
| 安装网络 | setup network |
| 团队策略 | team policy |
| 工作台 | workbench |
| Harness、Agent、Oracle、digest、SDK | unchanged |
