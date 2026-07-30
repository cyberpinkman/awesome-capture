# Contributing to Awesome Capture

感谢你帮助改进 Awesome Capture。本仓库把四个 standalone skills、确定性脚本、
canonical contracts、公开文档和测试作为同一个发行单元维护。Pull Request
不仅要说明“改了什么”，还要提供足够证据证明安全边界、契约兼容性和失败行为
没有被意外削弱。

安全漏洞或可能泄露 Cookie、token、私有媒体、模型路径等信息的问题，不要先
提交公开 Issue 或 PR；请按 [`SECURITY.md`](SECURITY.md) 的方式私下报告。

## 贡献许可

提交贡献即表示你有权提交这些内容，并同意贡献按本仓库的
[`MIT License`](LICENSE) 许可，采用与项目相同的 inbound=outbound 条款。
不要提交无权再许可的第三方代码、媒体、模型、transcript 或测试素材。本项目
当前不要求额外 CLA 或 DCO；这不减少贡献者确认来源与授权的责任。

## PR 工作流

1. 从最新 `main` 创建独立分支。普通改动不得直接 push 到 `main`。
2. 一个 PR 只解决一个可清楚描述的问题；不要顺带重构无关代码。
3. 涉及契约、安全边界、文件系统、并发、外部平台或 ASR 引擎时，建议先开
   Draft PR，尽早说明风险和验证方案。
4. 完成实现、测试、公开文档和 `[Unreleased]` changelog 后，运行本文列出的
   本地门禁。
5. 使用仓库 PR 模板提交证据。所有必需 CI 必须在同一个最终 head SHA 上通过。
6. 处理 review 后若继续修改，重新运行受影响测试；不能用旧 commit 的成功
   结果证明新 commit。

分支名建议使用 `feat/`、`fix/`、`docs/`、`test/`、`ci/`、`security/`
或自动化工具自己的明确前缀，例如 `fix/vault-lock-race`。

## PR 标题与范围

PR 标题使用下面的形式，合并时默认作为 squash commit 标题：

```text
type(scope): imperative summary
```

允许的 `type`：

- `feat`：新增向后兼容能力；
- `fix`：修复用户可见缺陷；
- `docs`：只修改公开文档；
- `test`：只增强验证；
- `ci`：修改工作流或发布门禁；
- `security`：安全加固；
- `chore`：不改变公开行为的维护工作。

`scope` 可省略；使用时优先写 skill 名、`contracts`、`smoke`、`release` 或
`ci`。标题描述结果，不写“update files”“misc fixes”等无法审计的摘要。

PR 正文必须明确：

- 动机、变更范围和明确不在范围内的内容；
- 用户可见行为、兼容性和失败模式是否变化；
- 验证命令、结果及未验证部分；
- 是否影响 wire schema、digest、持久路径、CLI 或支持基线；
- 是否需要真实平台/ASR smoke 证据。

## Changelog、版本与兼容性

- 面向使用者或维护者工作流的变化写入
  [`CHANGELOG.md`](CHANGELOG.md) 的 `[Unreleased]`。
- 普通 PR 不直接修改 `VERSION`；版本升级由明确的 release preparation PR
  按 [`VERSIONING.md`](VERSIONING.md) 决定。
- Breaking change 必须在 changelog 中同时提供 `Breaking` 和 `Migration`，
  并说明拒绝旧输入、数据保留和回滚边界。
- 仓库 SemVer、wire schema 版本、`contract_digest`、`runtime_digest`、
  `implementation_digest` 和 commit SHA 不可互相替代。

## 变更证据矩阵

所有 PR 合并前都必须满足“本地必需门禁”；作者应在本机运行适用项目，无法
复现的操作系统或受控环境项目必须在 PR 中明确标为未验证，并由 required CI
或维护者证据补齐。不得把“本机不可用”写成已通过。下列类型还要提供对应证据：

| 变更类型 | 额外要求 |
|---|---|
| 文档或治理 | 链接有效；命令、版本、路径和公开承诺与实现一致；相关仓库结构测试通过 |
| Python/CLI 行为 | 成功、预期失败、退出码和 stdout/stderr JSON 契约测试；不得引入运行时第三方 Python 依赖 |
| 路径、锁、并发、恢复 | 正向、冲突、symlink/hardlink、竞争和故障注入测试；说明 no-clobber 与恢复边界 |
| Canonical contract | 只改 `contracts/` 源码，再生成 vendored 副本；同步 producer、全部 consumer、fixtures、测试、SKILL/reference 和 breaking policy |
| 外部平台或 ASR | mock/fixture 测试之外，运行受影响路径的真实 smoke；只提交通过 schema、digest、脱敏和 outcome 校验的 receipt |
| CI 或发布流程 | 给出原失败 run/复现证据和根因；远程 Action 固定完整 commit SHA；不得通过放宽 no-skip、完整性或秘密门禁制造绿灯 |
| Release preparation | 额外遵循 [`RELEASING.md`](RELEASING.md)，审查并提交受影响组件 scope，验证所需 smoke 和确切 release commit |

### Canonical contract 规则

`contracts/` 是正式 schema、validator 和安全 runtime 的唯一源码。
`skills/*/scripts/_contracts/` 只能由下面的命令生成，不得手改：

```bash
python3 tools/sync_vendored.py --apply
python3 tools/sync_vendored.py --check
```

消费者必须使用自己的 vendored 副本重新验证，不能只相信上游
`status: complete`。任何 contract digest 变化都要评估 producer/consumer
兼容性和仓库版本影响。

## 本地必需门禁

从仓库根目录运行：

```bash
python3 tools/release.py check
python3 tools/sync_vendored.py --check
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip
PYTHONDONTWRITEBYTECODE=1 python3 tools/check_repository_hygiene.py
git diff --check
test -z "$(git status --porcelain)"
```

最后一条应在提交后、干净工作树上运行。测试必须为零 skip、零 unexpected
success；本地缺少必需工具不是跳过测试的理由，应先修复环境或明确报告阻塞。

若 PR 只修改少量文件，可以先运行目标测试缩短反馈时间，但 ready for review
前仍要运行本机可支持的全部门禁。

贡献者不需要自行准备全部 CI 操作系统。无法在本机运行完整套件时，应运行
目标测试和可用门禁，在 PR 中列出缺口，并等待仓库 required CI 的完整矩阵；
required check 未成功前仍不可合并。

## CI 失败处理

- 任一必需 check 失败、取消或仍在运行时，PR 不可合并。
- 不得通过反复 rerun 直到偶然变绿来关闭失败。记录首次失败的 run、job、
  step、根因和修复证据。
- 先区分实现缺陷、测试不确定性和外部基础设施故障。基础设施故障也要采用
  有界重试、明确 timeout 和失败关闭，不能无限等待或忽略错误。
- 并发或时序失败必须增加能证明根因修复的回归测试；单次成功不足以证明
  race 已消失。
- 修复后以最终 head SHA 的完整矩阵为准。旧 commit、旧 implementation
  digest 或旧 smoke receipt 的绿灯不能复用。

## 安全与公开仓库边界

提交前确认没有加入：

- Cookie、Authorization header、token、API key、签名 URL 或浏览器资料；
- 私有媒体、模型、transcript、原始日志、主机名、用户名或私有绝对路径；
- `.awesome-capture-media/`、artifact/state/journal、`__pycache__`、覆盖率
  文件、临时目录或未验证的 smoke receipt；
- 原始 extractor JSON 或可能包含临时 CDN 地址的调试输出。

公开 smoke receipt 只能包含正式 schema 允许的脱敏证据。真实媒体、模型和
Cookie 必须留在受控环境；普通 PR CI 不下载模型，也不接收任意 URL 或秘密。

### 受控真实 Smoke

- `.github/workflows/smoke.yml` 只允许原仓库的默认分支执行，不能从 fork、
  PR ref 或任意功能分支直接把代码送入持久 self-hosted runner。
- 所有执行 smoke case 的 GitHub job 必须绑定 `awesome-capture-smoke`
  Environment。维护者应在仓库设置中为该 Environment 配置 required
  reviewers；没有完成保护配置时，不得把它视为受控发布环境。
- PR 合并前需要验证功能分支时，使用同源本地 harness 或一次性隔离 runner，
  并由维护者先审查确切 commit；不得让 fork PR 自动获得媒体、模型、adapter
  路径或其他 runner 本地资源。
- 外部贡献者无法访问受控平台、媒体或模型时，在 PR 中标记
  `maintainer smoke required` 并提供可公开的复现边界；维护者负责在审查后的
  确切 commit 上补跑 smoke。不要交换 Cookie、私有媒体、模型或 runner 路径。
- 下载 smoke 必须在联网前把规范化 URL 与 `smoke/cases.json` 中预登记的脱敏
  SHA-256 指纹匹配；workflow 不接受任意 URL。修改样本指纹会改变当前实现
  digest，必须为新 digest 重新生成证据。
- `twitter-anonymous` 使用真实 `yt-dlp` 证明自然直连；
  `tiktok-gallery-fallback` 与 `twitter-gallery-fallback` 各自使用不可互换的
  registry 固定 fault profile，并在 receipt 中明确披露单次 `yt-dlp`
  `NETWORK_ERROR`，再让未修改的生产 fallback gate 调用真实 `gallery-dl`。
  两者都是回退韧性证据，不得表述为 TikTok 或 X 自然失败。不得新增接受任意
  故障命令、可执行路径，或 fault CLI、workflow、环境变量输入的入口。
- smoke receipt 只有在独立执行 `python3 tools/smoke_receipts.py validate`
  并传入 receipt 路径、`--require-current-digest`、`--require-single` 和
  `--require-case <alias>`，通过 schema、digest、case、脱敏和 outcome 复验
  后才可上传。每个 workflow attempt 使用独立目录；harness 失败可以保留
  合法的 `outcome: fail` 审计证据，但验证失败的 JSON 不得上传。
- 正式发布范围只能由候选 commit 中的 `smoke/release-scope.json` 声明，不能
  通过 workflow dispatch 临时缩小。Scope 的 `base_version` 和 `base_commit`
  必须绑定上一完整 release 边界；门禁会对 baseline 到候选 `HEAD` 的执行
  脚本、skill 行为规范、case registry 和 contract 变化保守推导最低组件，
  声明只能扩大、不能缩小。
  正式候选的基线版本必须严格更低、恰为 changelog 中紧邻的上一版本，并由
  对应不可移动轻量 tag 精确指向；仅未曾创建 tag/Release 的 `0.1.0` 历史
  版本边界使用代码中固定完整 SHA 的一次性 bootstrap。
  `external_impact: selected` 必须列出排序、唯一且已注册的受影响组件；
  `external_impact: none` 必须使用空列表，且只适用于机器下界也为空的版本。
- Release 门禁仍严格验证目录内每份 receipt 的 schema、语义、脱敏、已注册
  case 和 passing outcome；仅 scope 内 case 强制 current digest 与完整覆盖。
  聚合组件与其子组件不得重复声明。

## Review 与合并规则

- 作者完成模板、回应 review 并解决所有 blocking conversation。
- Reviewer 重点检查公开行为、全部消费者、失败路径、敏感输出和验证证据，
  不只检查 happy path。
- 默认使用 squash merge；PR 标题应能作为清晰的最终 commit 标题。
- PR 标题、正文证据和不适用项由 reviewer 人工复核；仓库不使用高权限
  `pull_request_target` 去执行或解析外部贡献代码。
- 合并前，最终 head SHA 的全部 required checks 必须成功，分支不得落后于
  影响结果的 `main` 变化。
- 远端 ruleset 应要求稳定汇总检查 `tests / required`；该检查只有在 Linux
  与 macOS 的完整 no-skip 矩阵全部成功时才通过。
- 不 force-push 或删除已发布 tag；发布错误通过新的更高版本修复。
- `main` 的远端 ruleset/branch protection 是此规范的执行层；没有远端保护
  时，维护者仍不得把直接 push 当作正常工作流。
