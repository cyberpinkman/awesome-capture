<!--
感谢贡献。请保留所有标题；不适用的部分写“不适用”并说明原因。不适用的
复选项保持未勾选，并在对应段落解释，不要把未验证事项勾成完成。完整规则见
CONTRIBUTING.md。不要在 PR 正文或附件中粘贴 Cookie、token、私有路径、
原始日志、媒体、模型或 transcript。
-->

## 变更摘要

<!-- 说明解决的问题、用户/维护者获得的结果。 -->

## 动机与范围

<!-- 为什么需要此变更？明确本 PR 包含和不包含什么。 -->

## 变更类型

- [ ] `feat`：新增能力
- [ ] `fix`：缺陷修复
- [ ] `docs`：公开文档
- [ ] `test`：测试增强
- [ ] `ci`：CI/发布流程
- [ ] `security`：安全加固
- [ ] `chore`：内部维护

## 契约与兼容性

<!--
说明 CLI、wire schema、digest、持久路径、支持基线和失败行为是否变化。
若修改 canonical contract，列出 producer、全部 consumer、fixtures、测试和
文档的同步情况。若为 breaking change，说明 Migration。
-->

- 用户可见行为：
- 兼容性/迁移：
- 版本影响：

## 验证证据

| 检查 | 命令或运行链接 | 结果 |
|---|---|---|
| Release metadata | `python3 tools/release.py check` | |
| Contract sync | `python3 tools/sync_vendored.py --check` | |
| No-skip tests | `PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip` | |
| Repository hygiene | `PYTHONDONTWRITEBYTECODE=1 python3 tools/check_repository_hygiene.py` | |
| Diff check | `git diff --check` | |

<!--
若曾有 CI 失败，填写首次失败 run/job/step、根因和修复证据；不要只写
“rerun passed”。未运行的检查必须说明原因和阻塞。
-->

- 首次失败与根因：
- 尚未验证：

## 真实 Smoke 证据

<!--
仅在影响平台 extractor、ASR 引擎或外部工具路径时填写。使用预登记 alias；
receipt 必须通过 schema、current digest、脱敏和 outcome 校验。不要粘贴原始
URL、Cookie、媒体、模型、transcript 或日志。不适用时写“不适用”。
-->

- Case alias：
- Receipt/验证结果：
- Auth/fallback/warnings：

## 安全与公开性

- [ ] 没有提交 Cookie、header、token、签名 URL、私有路径或原始日志
- [ ] 没有提交媒体、模型、transcript、运行 artifact/state/journal 或缓存
- [ ] 若涉及输出或错误：已验证脱敏、权限、no-clobber 与失败关闭规则
- [ ] 若涉及文件系统：已验证 symlink、hardlink、路径交换、并发和恢复风险
- [ ] 若涉及真实 smoke：fork/PR ref 未接触持久 runner，receipt 上传前已复验

## Changelog 与文档

- [ ] 面向使用者/维护者的变化已写入 `CHANGELOG.md` 的 `[Unreleased]`
- [ ] README、AGENTS、SKILL/reference 与实现保持一致
- [ ] 若为 breaking change：同时包含 `Breaking` 和 `Migration`
- [ ] 本 PR 不擅自修改 `VERSION`，或它是明确的 release preparation PR

## 提交前检查

- [ ] PR 只包含一个清楚目标，没有夹带无关重构
- [ ] 最终 head SHA 的 required checks 全部成功
- [ ] 没有通过跳过测试、忽略错误或反复 rerun 掩盖失败
- [ ] 所有 blocking review conversation 已解决
- [ ] 已阅读并遵守 `CONTRIBUTING.md`
