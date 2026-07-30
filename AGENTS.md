# Awesome Capture — Agent Bootstrap

本文件是陌生 Agent 接管仓库时的规范入口。目标是在不加载无关上下文的情况下，快速、正确地组合四个 skills。

## 0. Authority

- 用户只发送仓库链接：只授权读取、解释和提出方案。
- 用户要求安装：才可把 skill 复制到本地 skills 目录或安装依赖。
- 用户要求下载或转写：只授权处理用户指定的 URL 或本地文件。
- 用户调用 `ingest-knowledge`，或在转写后明确回答“是”：才授权写入指定 vault。
- 用户确认建库 plan：才授权 `vault_builder.py build ... --apply`。
- 读取指定浏览器或 Cookie 文件必须单独获得明确授权。

不要把“完成工作流”的要求扩大为读取个人浏览器、扫描整个 home、上传本地媒体、绕过登录或修改 Obsidian 全局配置。

## 1. Five-minute Reading Order

1. 完整阅读本文件。
2. 修改仓库代码、文档或 CI 时，完整阅读 `CONTRIBUTING.md`。
3. 浏览 `README.md` 的“四个 Skills”“Agent 层与脚本层”“文件契约”。
4. 根据任务路由，完整阅读目标 `skills/<name>/SKILL.md`。
5. 只读取该 `SKILL.md` 针对当前情形明确引用的 references。
6. 在执行外部工具或写文件前，先运行对应的 `doctor`、`inspect`、`validate` 或 `plan`。

`SKILL.md` 是行为规范；`scripts/` 是确定性执行层；`references/` 是按需加载的深入规则。不要只看脚本名猜测完整工作流。

## 2. Intent Routing

| 用户意图 | 首选 Skill | 可能的后续 Skill |
|---|---|---|
| 下载一个公开视频 | `download-video` | 无，除非用户还要求转写 |
| 转写本地音频或视频 | `transcribe-media` | 完成后询问是否 `ingest-knowledge` |
| 转写一个视频 URL | `download-video` → `transcribe-media` | 完成后询问是否 `ingest-knowledge` |
| 把已完成转写存入 Obsidian | `ingest-knowledge` | 若没有 vault，先 `build-obsidian-vault` |
| 新建或规范化 Obsidian 知识库 | `build-obsidian-vault` | 可供后续 `ingest-knowledge` 使用 |
| 批量下载、OCR、网页、PDF、事实核查 | 当前四个 skill 不覆盖 | 建议新增独立 skill |

不要把多个目标塞进一个脚本。组合发生在 Agent 层，脚本之间只传版本化 artifact 和明确路径。

## 3. Orchestration State Machines

### URL → Transcript

1. 调用 `download-video`。
2. 要求下载 artifact：
   - `schema_version == "awesome-capture.artifact/v2"`
   - `status == "complete"`
   - `media.has_video == true`
   - `media.path` 存在
3. 读取 artifact 的 `media.path` 和 artifact 自身绝对路径，不要猜下载文件名。
4. 把媒体路径与显式 `--source-artifact` 一起交给 `transcribe-media`；消费者必须重新验证 contract digest、媒体 hash 和 ffprobe 证据。
5. 要求 transcript artifact：
   - `schema_version == "awesome-capture.artifact/v2"`
   - `artifact_type == "transcript"`
   - `status == "complete"`
   - source hash 存在
   - timestamps 单调且不越界
6. 若有非空语音，询问一次：“是否需要把这份内容写入本地 Obsidian 知识库？”

`transcribe_media.py` 直接收到 URL 时会拒绝并提示使用 download skill；这是模块边界，不是缺陷。

### Transcript → Obsidian

1. 解析现有 vault 路径；没有路径时询问，不要扫描整个 home。
2. 用 `knowledge_writer.py validate-transcript` 校验证据。
   - 必须严格验证 transcript v2 的结构与内部语义；
   - 默认不读取 `source.path` 或伴随输出；源媒体已删除不阻止入库；
   - 只有用户明确要求 `--verify-source-media` 时才复验源媒体。
3. 完整阅读 transcript。
4. 按 `skills/ingest-knowledge/references/note-schema.md` 在 vault 外起草 Markdown：
   - 恰好一个 H1；
   - 至少两个 H2；
   - 重要结论带时间戳；
   - 推断明确标为推断；
   - 缺少证据的内容放入“待验证”。
5. 先执行 `commit ... --dry-run`，保存返回的 `plan_sha256`。
6. 用户已明确授权后，移除 `--dry-run` 并传入 `--expected-plan-sha256`。
7. 重复写入必须返回 `reused`；不得制造重复笔记。

`knowledge_writer.py` 不负责生成摘要或观点；Agent 负责基于完整证据起草。

### Interview → Vault

1. 依次访谈目的/产出、输入/检索、维护成本、命名/链接/附件/同步和目标路径。
2. 读取 `profiles.md`，选择最接近的最小 profile。
3. 在 vault 外生成 `awesome-capture.vault-config/v1` JSON。
4. `validate-config`。
5. `plan`，向用户展示路径、文件夹、模板、链接风格和冲突。
6. 获得确认后 `build --apply --expected-plan-sha256 <plan_sha256>`。
7. `audit`。

`vault_builder.py` 不执行访谈，也不启动 Obsidian。

## 4. Contract Map

### A. Formal Contract Bundle

Canonical schemas、stdlib validator、POSIX runtime 和 fixtures 位于根目录 `contracts/`。每个 skill 的 `scripts/_contracts/` 是由 `tools/sync_vendored.py --apply` 生成的 standalone 副本，不得手改。manifest 分别记录 wire schema/validator 的 `contract_digest` 与安全实现的 `runtime_digest`；加载时两组都必须通过本地 hash 复验。CI 使用 `--check` 拒绝任何副本漂移。

- Video/Transcript：`awesome-capture.artifact/v2`，通过 `artifact_type` 区分。
- 转写 state：`awesome-capture.transcription-state/v1`。
- 完整 chunks：`awesome-capture.chunk-set/v1`。
- 恢复 journal：`awesome-capture.transaction/v1`。
- 建库 receipt：`awesome-capture.vault-build-receipt/v1`。
- 入库 receipt：`awesome-capture.ingest-receipt/v1`。
- Smoke receipt：`awesome-capture.smoke-receipt/v1`。

这是严格 breaking 切换：artifact/v1、无版本 state/receipt 和未知版本全部拒绝，不迁移、不双读、不覆盖。所有消费者都要重新执行结构、语义和已获授权的文件证据检查，不能只相信 `status: complete`。producer/consumer 的 `contract_digest` 必须一致。

- Video 交接核心：`status`、来源 fingerprint、`media.path/bytes/sha256`、整数毫秒时长和流证据。
- Transcript 交接核心：私有 source snapshot、内容级 engine/model/adapter identity、chunk-set、严格 `segments[]`、确定性 `text` 和全部输出 hashes。
- 下游只读取显式给出的 artifact 路径；不得通过目录扫描或文件名规则猜测。
- Cookie 值、API key、私有 header 和签名查询参数不得进入 artifact。

完整 transcript 规则：`skills/transcribe-media/references/artifact-contract.md`。

### B. `awesome-capture.vault-config/v1`

用于建库，不是媒体 artifact。配置 schema：

`skills/build-obsidian-vault/references/config-schema.md`

构建回执使用 `awesome-capture.vault-build-receipt/v1`：

`<vault>/.awesome-capture/vault-build.json`

### C. Ingest Receipt

用于知识入库幂等性，不是 artifact 或 vault config：

`<vault>/.awesome-capture/receipts/<stable-id>.json`

稳定 ID 基于完整 transcript artifact SHA-256，而不是源媒体 hash。相同 transcript 和 draft/layout 应复用 receipt；不同 transcript 修订得到新 ID。存在路径但正式 receipt、笔记 identity 或预期相对路径不匹配时必须报冲突。

## 5. Repository-root Commands

所有命令默认从仓库根目录运行。

### Pull Request governance

普通代码、契约、文档和 CI 变更必须从独立分支通过 PR 合并；不得把直接 push
到 `main` 当作默认工作流。PR 使用 `.github/PULL_REQUEST_TEMPLATE.md`，
完整标题、证据矩阵、CI 失败处理和合并规则见 `CONTRIBUTING.md`。

任一 required check 失败、取消或仍在运行时不得合并。不得反复 rerun 直到
偶然变绿；应记录首次失败 run/job/step、区分实现/测试/基础设施根因，并以
最终 head SHA 的完整矩阵证明修复。外部平台或 ASR 变化仍须提供受控真实
smoke 证据，不能把普通 PR 的离线绿灯当作发布证据。

持久 self-hosted smoke runner 不执行 fork、PR ref 或任意功能分支代码。
GitHub smoke 只从原仓库默认分支进入 `awesome-capture-smoke` Environment；
功能分支需要真实验证时使用同源本地 harness 或一次性隔离 runner。receipt
必须在上传前独立通过 schema、current digest、脱敏和 outcome 复验。

下载 smoke 在联网前必须把规范化 URL 与 registry 中预登记的脱敏 SHA-256
指纹匹配，workflow 不接收任意 URL。`twitter-anonymous` 用真实 `yt-dlp`
证明自然直连；`tiktok-gallery-fallback` 与 `twitter-gallery-fallback`
各自绑定不可互换的 registry 固定 fault profile，使用 receipt 明确披露的单次
`yt-dlp` `NETWORK_ERROR`，再让未修改的生产 fallback gate 调用真实
`gallery-dl`。两者只证明回退韧性，不证明 TikTok 或 X 自然失败。不得提供
任意故障命令、可执行路径，或 fault CLI、workflow、环境变量输入。

### Release metadata

仓库发行版本由根目录 `VERSION` 唯一维护，四个
`skills/<name>/VERSION` 是 standalone 分发副本；四个 skills 必须锁步发布。
Git tag 使用 `vX.Y.Z`，版本文件使用 `X.Y.Z`。`main` 是未发布开发分支，
普通安装应固定到 GitHub Release tag。

仓库 SemVer 不等同于 wire schema 版本、`contract_digest`、
`runtime_digest`、`implementation_digest` 或 commit SHA。完整边界见
`VERSIONING.md`；发布流程见 `RELEASING.md`。修改用户可见行为时先更新
`CHANGELOG.md` 的 `[Unreleased]`，不要把 breaking change 隐藏在 patch 中。

```bash
python3 tools/release.py check
```

正式 Release 使用候选 commit 中已审查的 `smoke/release-scope.json` 声明受
影响组件，并运行 `validate-release`。Scope 绑定上一 release 的版本和 commit；
门禁从该基线到候选 `HEAD` 保守推导下载/转写脚本、skill 行为规范、case
registry 和共享 contract 的最低组件，声明只能扩大、不能缩小。正式候选的
基线必须严格低于候选版本、恰为 changelog 中紧邻的上一版本，并由对应不可
移动轻量 tag 精确指向；仅未曾创建 tag/Release 的 `0.1.0` 历史版本边界使用
代码中固定完整 SHA 的一次性 bootstrap。所有 receipt 仍严格复验 schema、
语义、脱敏、注册身份和 passing outcome；只有 scope 映射出的 case 必须覆盖
完整且匹配当前 implementation digest。普通 PR CI 可在没有真实外部 smoke
时运行。

### Unified CLI JSON protocol

四个 skill 的 CLI 成功时 stdout 恰好输出一个 JSON object、stderr 为空并退出 0；预期失败时 stdout 为空、stderr 恰好输出一个脱敏 JSON error。统一退出码为：

| 退出码 | 含义 |
|---:|---|
| `2` | 参数、schema 或不安全输入/路径 |
| `3` | 依赖、模型或平台能力不可用 |
| `4` | 锁繁忙、冲突、恢复冲突或计划已过期 |
| `5` | 外部工具、网络或运行时 I/O 失败 |
| `7` | 契约、身份或文件证据完整性失败 |
| `130` | 用户或系统中断 |

```bash
# 生成式 contract 副本必须与 canonical 完全一致
python3 tools/sync_vendored.py --check

# 全套离线测试；任何 skip 或 unexpected success 都失败
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip

# 下载环境
python3 skills/download-video/scripts/download_video.py doctor

# 平台检测
python3 skills/download-video/scripts/download_video.py detect "<url>"

# 转写环境
python3 skills/transcribe-media/scripts/transcribe_media.py doctor \
  --model "/absolute/path/to/model.bin" \
  --whisper-cpp-bin "/absolute/path/to/whisper-cli"

# 媒体检查
python3 skills/transcribe-media/scripts/transcribe_media.py inspect \
  "/absolute/path/to/media"

# URL 下载结果必须显式交接 artifact；本地直传媒体可不带
python3 skills/transcribe-media/scripts/transcribe_media.py transcribe \
  "/absolute/path/to/media" \
  --output-dir "/absolute/path/to/output" \
  --source-artifact "/absolute/path/to/video.artifact.json" \
  --engine auto \
  --model "/absolute/path/to/model.bin" \
  --whisper-cpp-bin "/absolute/path/to/whisper-cli"

# Vault 配置检查和预览
python3 skills/build-obsidian-vault/scripts/vault_builder.py validate-config \
  "/absolute/path/to/config.json"
python3 skills/build-obsidian-vault/scripts/vault_builder.py plan \
  "/absolute/path/to/config.json" \
  --vault "/absolute/path/to/vault"

# 崩溃后先恢复，再重试写操作
python3 skills/download-video/scripts/download_video.py recover \
  --output-dir "/absolute/path/to/output"
python3 skills/transcribe-media/scripts/transcribe_media.py recover \
  --output-dir "/absolute/path/to/output"
python3 skills/build-obsidian-vault/scripts/vault_builder.py recover \
  --vault "/absolute/path/to/vault"
python3 skills/ingest-knowledge/scripts/knowledge_writer.py recover \
  --vault "/absolute/path/to/vault"

# Transcript 证据检查
python3 skills/ingest-knowledge/scripts/knowledge_writer.py validate-transcript \
  "/absolute/path/to/transcript.json"

# Smoke receipt 只保存脱敏证据
python3 tools/smoke_receipts.py digest
python3 tools/smoke_receipts.py validate \
  "/absolute/path/to/smoke-receipt.json" \
  --require-pass --require-current-digest
python3 tools/smoke_receipts.py components
python3 tools/smoke_receipts.py validate-release
```

skill 自己的 `SKILL.md` 中使用 `python3 scripts/...`，那是以 skill 目录为当前工作目录的写法。

## 6. Non-negotiable Invariants

### Download

- 写操作只支持 Python 3.11–3.14 的 macOS/Linux POSIX 环境；缺失 no-follow、dir-fd、`fcntl`、目录 `fsync`、原子 no-replace rename 或原子 exchange rename 时失败关闭。
- 仅接受 Douyin、TikTok、Bilibili、YouTube、X/Twitter 的精确 host 后缀。
- 默认单条视频；不得隐式展开 playlist。
- `yt-dlp --ignore-config` 保持启用。
- 缩略图、metadata、空文件、无视频流文件不算成功。
- 外部下载器只能写私有 staging；子进程工作目录固定到已持有的 staging 目录 FD。转写媒体输入尽可能通过继承的只读 FD 交接。验证 bytes/hash/ffprobe 后才发布，artifact 必须最后写入。
- 复用只接受内容与当前文件证据完全匹配的 artifact/v2；不得复用目录中预种或猜到的媒体。
- 匿名失败后只允许平台规定的有限回退；不得无限重试。
- 不绕过 DRM、付费、私密、地区或授权限制。

### Transcription

- 本地媒体不得被静默上传到远程服务。
- 不下载模型；每个 ASR 引擎都必须使用显式本地模型并记录确定性内容 hash。
- whisper.cpp binary、external adapter 和本地模型必须在发布前重新哈希；运行期间身份变化必须中止。
- `auto` 只选择显式配置的 whisper.cpp；external adapter 必须有用户的 `--trust-external-adapter` 确认。
- 标题、简介和 LLM 重建不属于语音转写。
- chunks 必须作为完整、连续、有 manifest 的集合整体发布；缺失、额外、断号或 hash 不匹配不能解释为空语音。
- 续跑 state 必须绑定来源、引擎、模型、adapter、设置和 chunk-set hash。
- whisper.cpp GPU 失败必须隔离，并允许 CPU 回退；不得把崩溃当空转写。

### Vault and Ingest

- 先 preview/dry-run，再把返回的 `plan_sha256` 作为 `--expected-plan-sha256` 写入。
- 不覆盖不同内容。
- 拒绝 root、home、`.obsidian`、父路径穿越和受管目录 symlink。
- build 与 ingest 共用持久 vault lock；journal 先落盘，receipt 最后发布。发现未完成事务时先 recover。
- audit 只读：builder 检查正式 build receipt 与受管布局，knowledge writer 检查 ingest receipts、笔记 identity 和 pending transactions。
- 不写 Obsidian 全局注册表，不启用 Sync，不安装社区插件。
- 普通 Markdown/YAML 和 skill 自有 receipt 是持久化边界。

## 7. Definition of Done

### 修改代码

- 目标行为有测试；
- 版本元数据通过 `python3 tools/release.py check`；面向使用者的变化已写入
  `CHANGELOG.md` 的 `[Unreleased]`；
- `python3 tools/sync_vendored.py --check` 通过；
- `PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip` 全部通过且零 skip；
- 未产生 `__pycache__`、模型、媒体、Cookie 或临时输出；
- 修改契约时只改 canonical，再生成 vendored 副本，并同步更新生产者、全部消费者、fixtures、测试、`SKILL.md` 和 reference；
- 外部平台/引擎变化有真实 smoke test 和符合 `awesome-capture.smoke-receipt/v1` 的脱敏 receipt，不能只靠 mock。

### 下载任务

- 返回绝对媒体路径和 artifact 路径；
- artifact/v2 为 complete 且 consumer 可重新验证 contract digest；
- `ffprobe` 验证视频流；
- 报告实际 auth mode、fallback 和 warnings。

### 转写任务

- 返回完整 transcript artifact/v2 和所有带 bytes/hash 的文本、字幕、state 路径；
- 报告引擎、语言、时长、segment 数和 GPU fallback；
- 非空语音才询问是否入库。

### 入库任务

- 返回知识笔记、原始转写和 receipt 的绝对路径；
- receipt 使用 `awesome-capture.ingest-receipt/v1`，稳定 ID 为完整 64 位 digest；
- 报告 `created` 或 `reused`；
- vault audit 无异常，或明确列出既有异常。

### 建库任务

- 报告 vault、receipt、created/skipped/conflicts；
- build 后运行 audit；
- 告知用户唯一必要手动动作：在 Obsidian 中打开该目录；
- 若启用 daily notes，只创建目录和模板，不声称已经配置核心插件。

## 8. Safe Extension Pattern

优先新增一个窄 skill，而不是扩大现有 skill：

1. 定义输入、输出、成功判据和副作用；
2. 判断能否消费现有 artifact；
3. 若不能，新增版本化契约，不猜目录；
4. 在 `contracts/` 更新 canonical schema/runtime，并用 `tools/sync_vendored.py --apply` 生成每个 standalone 副本；
5. 实现确定性 script，并让全部消费者独立复验；
6. 编写正向、负向、冲突、恢复和安全测试；
7. 更新本文件的路由与契约图；
8. 用真实代表性输入验证并产生脱敏 smoke receipt；
9. 明确尚未支持的边界。

推荐后续独立 skills：`download-batch`、`extract-webpage`、`extract-document`、`run-ocr`、`fact-check-content`。

## 9. Known Tested Baseline

`0.1.0`（2026-07-28）是未创建 tag/Release 的历史 SemVer 版本边界。下列
工具版本与测试结果是该版本准备时的历史可复现基线，不代替当前实现的发布
证据：

- 支持 Python 3.11–3.14；CI 覆盖 Ubuntu 3.11–3.14 与 macOS 3.11、3.14，全部使用 no-skip runner；
- `awesome-capture.artifact/v2` 为当前唯一可接受的媒体 artifact 版本；v1 是明确拒绝的 legacy 输入；
- canonical/vendored contract sync 是 CI 门禁；
- `yt-dlp 2026.07.04`；
- FFmpeg/FFprobe 8.1；
- Deno 2.9.4；
- `gallery-dl 1.32.8`；
- Playwright 1.60.0 + 隔离 Chromium；
- `whisper.cpp 1.9.1`。

版本号是可复现基线，不是永久上限。路径验证应覆盖受影响平台与引擎；正式
Release 按已审查的 release scope 要求对应预登记 case 具有 passing 且匹配
当前 implementation digest 的 `awesome-capture.smoke-receipt/v1`，不强迫
未受影响组件重跑。receipt 时间仅用于审计，不设固定过期门槛，也不得用历史
实现的 receipt 替代当前实现证据。
