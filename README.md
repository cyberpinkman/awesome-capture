# Awesome Capture

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/cyberpinkman/awesome-capture/tests.yml?branch=main&style=flat-square&logo=githubactions&logoColor=white&label=tests)](https://github.com/cyberpinkman/awesome-capture/actions/workflows/tests.yml)

**支持的视频平台**

[![Douyin](https://img.shields.io/badge/Douyin-000000?style=flat-square&logo=tiktok&logoColor=white)](skills/download-video/SKILL.md)
[![TikTok](https://img.shields.io/badge/TikTok-000000?style=flat-square&logo=tiktok&logoColor=white)](skills/download-video/SKILL.md)
[![Bilibili](https://img.shields.io/badge/Bilibili-00A1D6?style=flat-square&logo=bilibili&logoColor=white)](skills/download-video/SKILL.md)
[![YouTube](https://img.shields.io/badge/YouTube-FF0000?style=flat-square&logo=youtube&logoColor=white)](skills/download-video/SKILL.md)
[![X / Twitter](https://img.shields.io/badge/X%20%2F%20Twitter-000000?style=flat-square&logo=x&logoColor=white)](skills/download-video/SKILL.md)

**运行环境与核心本地工具**

[![Python](https://img.shields.io/badge/Python-3.11--3.14-3776AB?style=flat-square&logo=python&logoColor=white)](#运行依赖)
[![macOS](https://img.shields.io/badge/macOS-POSIX-000000?style=flat-square&logo=macos&logoColor=white)](#运行依赖)
[![Linux](https://img.shields.io/badge/Linux-POSIX-FCC624?style=flat-square&logo=linux&logoColor=black)](#运行依赖)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-download-333333?style=flat-square&logo=github&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-media-007808?style=flat-square&logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![whisper.cpp](https://img.shields.io/badge/whisper.cpp-ASR-00599C?style=flat-square&logo=cplusplus&logoColor=white)](https://github.com/ggml-org/whisper.cpp)
[![faster-whisper](https://img.shields.io/badge/faster--whisper-ASR-3776AB?style=flat-square&logo=python&logoColor=white)](https://github.com/SYSTRAN/faster-whisper)
[![MLX Whisper](https://img.shields.io/badge/MLX%20Whisper-ASR-000000?style=flat-square&logo=apple&logoColor=white)](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
[![Obsidian](https://img.shields.io/badge/Obsidian-vault-7C3AED?style=flat-square&logo=obsidian&logoColor=white)](https://obsidian.md/)

徽章表示当前支持范围或可选的本地集成，不代表本仓库捆绑第三方二进制、模型或平台服务。

一组面向本地 AI Agent 的模块化信息捕获 skills：下载公开视频、转写音视频、把内容结构化写入 Obsidian，以及按用户习惯搭建 Obsidian 知识库。

> 安全支持范围：macOS / Linux（POSIX），Python 3.11–3.14。
> 当前能力边界：单个公开视频、本地音视频、本地 Obsidian vault；不支持 DRM、付费、私密内容或登录绕过。

仓库已经公开，当前版本元数据为 `0.1.0`，但尚未创建 Git tag 或
[GitHub Release](https://github.com/cyberpinkman/awesome-capture/releases)；
因此目前没有可声明为稳定、不可变的 release tag。`main` 是公开开发分支，
可能继续变化；当前使用者应记录实际检出的 commit SHA。

[贡献指南](CONTRIBUTING.md) · [更新记录](CHANGELOG.md) ·
[版本策略](VERSIONING.md) · [发布流程](RELEASING.md) ·
[安全策略](SECURITY.md)

## 给本地 Agent 的快速指引

把仓库链接发给本地 Agent 时，可直接附上下面这段话：

```text
请先完整阅读仓库根目录的 AGENTS.md。
然后根据我的任务，完整阅读对应的 skills/<skill-name>/SKILL.md；
只加载该 SKILL.md 明确引用的 references，不要先运行脚本。
严格区分 Agent 编排层与确定性脚本层，遵守 artifact 交接协议、
用户确认要求、无覆盖写入和失败判据。执行前先运行对应 doctor/plan，
完成后运行仓库测试并报告实际产物路径。
```

根目录的 [AGENTS.md](AGENTS.md) 是面向 Agent 的规范入口，包含：

- 五分钟阅读顺序；
- 用户意图到 skill 的路由表；
- 四个 skill 的组合状态机；
- artifact、vault config 和 receipt 的区别；
- 安全约束、完成判据及验证命令；
- 新增 skill 时必须保持的扩展规则。

仅发送仓库链接默认只授权 Agent 阅读和分析，不代表授权其安装依赖、读取浏览器 Cookie、下载内容或写入知识库。

## 四个 Skills

| Skill | 单一职责 | 主要输出 |
|---|---|---|
| [`download-video`](skills/download-video/SKILL.md) | 识别并下载单条 Douyin、TikTok、Bilibili、YouTube、X/Twitter 视频 | 可播放媒体、脱敏来源 metadata、已验证 video artifact |
| [`transcribe-media`](skills/transcribe-media/SKILL.md) | 把本地音视频转成带时间戳文本 | JSON、Markdown、TXT、SRT、VTT、续跑状态 |
| [`ingest-knowledge`](skills/ingest-knowledge/SKILL.md) | 把已核查转写整理并无覆盖地写入 Obsidian | 知识笔记、原始转写笔记、幂等 receipt |
| [`build-obsidian-vault`](skills/build-obsidian-vault/SKILL.md) | 通过访谈生成轻量、可维护的 Obsidian 知识库 | 文件夹、Markdown 模板、构建 receipt |

每个 skill 都能单独使用。组合时不直接导入另一个 skill 的 Python 代码，而是通过版本化 JSON artifact 和明确路径交接。

## 组合工作流

```text
视频 URL
  │
  ▼
download-video
  │  complete video artifact
  ▼
transcribe-media
  │  complete transcript artifact
  ▼
Agent 阅读全文并按 note schema 起草
  │
  ├── 用户不同意写入 ──► 停止
  │
  └── 用户明确同意
         ▼
   ingest-knowledge
         │
         ▼
   Obsidian 知识笔记 + 原始转写 + 幂等 receipt
```

如果用户还没有知识库：

```text
用户目标与使用习惯
  ▼
Agent 多轮访谈
  ▼
awesome-capture.vault-config/v1
  ▼
plan → 用户确认 → build → audit
  ▼
本地 Obsidian vault
```

### Agent 层与脚本层

这是理解项目最重要的边界：

| Agent 编排层负责 | 确定性脚本层负责 |
|---|---|
| 解释用户意图、选择 skill、请求必要确认 | URL/文件校验、平台检测、媒体探测 |
| URL 转写时先调用下载 skill | 下载单个视频并验证媒体流 |
| 阅读完整转写并起草知识文档 | 生成时间戳转写 artifact |
| 对用户进行建库访谈并生成 config | 校验、预览、构建、审计 vault |
| 判断是否继续下一个 skill | 原子写入、冲突拒绝、幂等复用 |

因此：

- `transcribe_media.py` 故意不接受 URL；Agent 必须先调用 `download-video`。
- `knowledge_writer.py` 故意不让模型自动概括；Agent 必须先阅读转写并按 schema 起草。
- `vault_builder.py` 故意不模拟对话；Agent 必须先访谈，再生成配置。

## 快速安装

### 1. 获取当前公开源码

```bash
git clone --branch main --depth 1 \
  https://github.com/cyberpinkman/awesome-capture.git
cd awesome-capture
git rev-parse HEAD
```

请保存最后一条命令输出的完整 SHA，作为本次安装的精确代码身份。仓库创建
首个正式 [GitHub Release](https://github.com/cyberpinkman/awesome-capture/releases)
后，稳定安装将改为检出对应不可移动 tag；在此之前不要把浮动的 `main`
描述成可复现稳定版。

### 2. 注册到 Codex

先确认目标目录中不存在同名 skill；不要静默覆盖已有版本：

```bash
mkdir -p ~/.codex/skills

for name in download-video transcribe-media ingest-knowledge build-obsidian-vault; do
  test ! -e "$HOME/.codex/skills/$name" || {
    echo "目标已存在，请先比较版本：$HOME/.codex/skills/$name"
    exit 1
  }
done

cp -R \
  skills/download-video \
  skills/transcribe-media \
  skills/ingest-knowledge \
  skills/build-obsidian-vault \
  ~/.codex/skills/
```

重新打开一个 Codex 任务后，可使用：

```text
$download-video
$transcribe-media
$ingest-knowledge
$build-obsidian-vault
```

其他支持 `SKILL.md` 的 Agent，请把这四个目录复制到该 Agent 的 skills 根目录。不同 Agent 产品的自动发现机制可能不同；若无法自动发现，让 Agent 直接读取根目录 `AGENTS.md` 和目标 `SKILL.md`。

## 运行依赖

所有运行时 Python 脚本只使用标准库。安全文件系统实现要求 Python 3.11–3.14，以及 `fcntl`、`dir_fd`、`O_NOFOLLOW`、目录 `fsync`、原子 no-replace rename 和原子 exchange rename 等 POSIX 能力；缺少任一能力时会失败关闭，不提供弱化的兼容模式。外部媒体能力按需安装：

| 能力 | 必需依赖 | 可选增强 |
|---|---|---|
| 视频下载 | `yt-dlp >= 2026.07.04`、`ffmpeg`、`ffprobe` | Deno、`gallery-dl >= 1.32.8`、Playwright Chromium、`curl-cffi` |
| 本地转写 | `ffmpeg`、`ffprobe`，以及一个明确选择的 ASR 引擎和本地模型 | `whisper.cpp`、`faster-whisper`、MLX Whisper、显式可信 external adapter |
| Obsidian 入库 | Python 3.11–3.14、一个现有本地 vault | 无 |
| Obsidian 建库 | Python 3.11–3.14 | Obsidian 桌面应用只用于用户之后打开 vault |

本项目不会自动下载模型，不会自动读取个人浏览器配置，也不会静默切换到远程转写服务。所有 ASR 引擎都要求显式本地模型；模型、binary 和 external adapter 以内容 SHA-256 标识。`--engine auto` 只会在同时提供本地 whisper.cpp 模型与 binary 时选择 whisper.cpp。

各本地引擎的准确参数、离线约束、内容身份和 external adapter 信任边界见
[`skills/transcribe-media/references/engines.md`](skills/transcribe-media/references/engines.md)。

在仓库根目录检查环境：

```bash
python3 skills/download-video/scripts/download_video.py doctor

python3 skills/transcribe-media/scripts/transcribe_media.py doctor \
  --model "/absolute/path/to/ggml-model.bin" \
  --whisper-cpp-bin "/absolute/path/to/whisper-cli"
```

转写 `doctor` 中：

- `ready_for_inspection: true` 只说明可以检查媒体；
- 没有同名字幕文件时，必须有 `ready_for_asr: true` 才能进行语音识别。

## 直接运行脚本

Skill 文档里的 `python3 scripts/...` 命令以该 skill 目录为工作目录。若从仓库根目录执行，必须使用下面的完整相对路径。

### CLI JSON 协议与退出码

四个 skill 的命令行入口使用同一机器可读协议：成功时 stdout 恰好输出一个 JSON object、stderr 为空并退出 0；预期失败时 stdout 为空、stderr 恰好输出一个脱敏 JSON error。不要通过解析普通日志判断结果。

| 退出码 | 含义 |
|---:|---|
| `2` | 参数、schema 或不安全输入/路径 |
| `3` | 依赖、模型或平台能力不可用 |
| `4` | 锁繁忙、冲突、恢复冲突或计划已过期 |
| `5` | 外部工具、网络或运行时 I/O 失败 |
| `7` | 契约、身份或文件证据完整性失败 |
| `130` | 用户或系统中断 |

### 检测和下载

```bash
python3 skills/download-video/scripts/download_video.py detect "<video-url>"

python3 skills/download-video/scripts/download_video.py download "<video-url>" \
  --output-dir "/absolute/output/directory"

# 中断或进程崩溃后，在重试前恢复已验证的受管事务
python3 skills/download-video/scripts/download_video.py recover \
  --output-dir "/absolute/output/directory"
```

下载成功必须同时满足：

- 进程退出码为 0；
- artifact 使用 `awesome-capture.artifact/v2` 且 `status` 为 `complete`；
- `media.has_video` 为 `true`；
- `media.path` 存在且已经过 `ffprobe`；
- 媒体文件有 SHA-256。

### 转写本地媒体

```bash
python3 skills/transcribe-media/scripts/transcribe_media.py inspect \
  "/absolute/path/to/media.mp4"

python3 skills/transcribe-media/scripts/transcribe_media.py transcribe \
  "/absolute/path/to/media.mp4" \
  --output-dir "/absolute/output/directory" \
  --source-artifact "/absolute/path/to/video.artifact.json" \
  --engine auto \
  --model "/absolute/path/to/ggml-model.bin" \
  --whisper-cpp-bin "/absolute/path/to/whisper-cli"

python3 skills/transcribe-media/scripts/transcribe_media.py recover \
  --output-dir "/absolute/output/directory"
```

URL 输入由 Agent 编排：先下载，读取 video artifact 的 `media.path`，再同时把该本地路径和明确的 `--source-artifact` 传给转写脚本。转写会重新验证 schema、contract digest、媒体 hash 和 ffprobe 证据；不会猜测相邻文件名。用户直接提供本地媒体时可以不传 `--source-artifact`。

### 建立 Obsidian Vault

可从仓库提供的
[`vault-config.example.json`](skills/build-obsidian-vault/assets/vault-config.example.json)
复制一份配置，再按
[`config-schema.md`](skills/build-obsidian-vault/references/config-schema.md)
调整目录、链接风格和 daily notes：

```bash
cp skills/build-obsidian-vault/assets/vault-config.example.json \
  "/absolute/path/to/vault-config.json"

python3 skills/build-obsidian-vault/scripts/vault_builder.py validate-config \
  "/absolute/path/to/vault-config.json"

python3 skills/build-obsidian-vault/scripts/vault_builder.py plan \
  "/absolute/path/to/vault-config.json" \
  --vault "/absolute/path/to/new-vault"

python3 skills/build-obsidian-vault/scripts/vault_builder.py build \
  "/absolute/path/to/vault-config.json" \
  --vault "/absolute/path/to/new-vault" \
  --expected-plan-sha256 "<plan 输出的 plan_sha256>" \
  --apply

python3 skills/build-obsidian-vault/scripts/vault_builder.py audit \
  --vault "/absolute/path/to/new-vault" \
  --require-build-receipt

python3 skills/build-obsidian-vault/scripts/vault_builder.py recover \
  --vault "/absolute/path/to/new-vault"
```

必须在向用户展示 `plan` 并获得确认后才能 `--apply`；写入命令必须带回同一次预览返回的 `plan_sha256`。脚本会在独占 vault 锁内重新计算，过期计划以 `STALE_PLAN` 失败。

### 写入知识库

```bash
python3 skills/ingest-knowledge/scripts/knowledge_writer.py validate-transcript \
  "/absolute/path/to/transcript.json"

python3 skills/ingest-knowledge/scripts/knowledge_writer.py commit \
  --transcript "/absolute/path/to/transcript.json" \
  --document "/absolute/path/to/agent-draft.md" \
  --vault "/absolute/path/to/vault" \
  --title "知识笔记标题" \
  --collection "00 Inbox" \
  --dry-run

# 用户确认后，使用 dry-run 返回的 plan_sha256 正式提交
python3 skills/ingest-knowledge/scripts/knowledge_writer.py commit \
  --transcript "/absolute/path/to/transcript.json" \
  --document "/absolute/path/to/agent-draft.md" \
  --vault "/absolute/path/to/vault" \
  --title "知识笔记标题" \
  --collection "00 Inbox" \
  --expected-plan-sha256 "<dry-run 输出的 plan_sha256>"

python3 skills/ingest-knowledge/scripts/knowledge_writer.py recover \
  --vault "/absolute/path/to/vault"

python3 skills/ingest-knowledge/scripts/knowledge_writer.py audit \
  --vault "/absolute/path/to/vault"
```

确认 dry-run 结果后才会写入。入库身份由完整 transcript artifact 内容计算；相同 transcript 再次写入应返回 `result: reused`，不同模型、语言或修订后的 transcript 会得到不同身份。默认只复验 transcript 内部证据，不要求源媒体仍存在；只有显式 `--verify-source-media` 才重新读取源媒体。

## 文件契约

当前版本是一次有意的破坏性升级：`awesome-capture.artifact/v1`、无版本 state 和旧 receipt 均被严格拒绝；没有双读、猜测或静默迁移。生产者与全部消费者必须使用同一 contract bundle，并复验结构、跨字段语义和自己获授权读取的文件证据。

### Canonical contract bundle

- 唯一源码位于 [`contracts/`](contracts/)；正式 wire schema 使用 JSON Schema Draft 2020-12。
- `tools/sync_vendored.py --apply` 把 runtime、manifest 和 schemas 生成到每个 `skills/<name>/scripts/_contracts/`，保证单独复制 skill 后仍可运行。manifest 将 wire schema/validator 的 `contract_digest` 与安全实现的 `runtime_digest` 分开记录。
- 修改 canonical 文件后必须执行 `python3 tools/sync_vendored.py --apply`；提交与 CI 使用 `python3 tools/sync_vendored.py --check` 拒绝漂移。
- artifact/receipt 中的 `producer.contract_digest` 必须与消费者本地 manifest 的 wire-contract digest 一致，否则返回 `CONTRACT_BUILD_MISMATCH`；加载 bundle 时还会独立验证本地 `runtime_digest`，防止 vendored 安全运行层漂移。
- 严格 JSON loader 拒绝重复 key、NaN/Infinity、超限输入、未知字段和未知版本。

正式版本：

| 契约 | 版本 |
|---|---|
| Video / Transcript Artifact | `awesome-capture.artifact/v2` |
| 转写续跑状态 | `awesome-capture.transcription-state/v1` |
| 完整分块集合 | `awesome-capture.chunk-set/v1` |
| 崩溃恢复 journal | `awesome-capture.transaction/v1` |
| Vault 构建 receipt | `awesome-capture.vault-build-receipt/v1` |
| 知识入库 receipt | `awesome-capture.ingest-receipt/v1` |
| 脱敏 smoke receipt | `awesome-capture.smoke-receipt/v1` |
| Vault 配置 | `awesome-capture.vault-config/v1` |

Video v2 记录脱敏来源指纹、媒体 bytes/hash、整数毫秒时长、视频/音频流数量、实际授权与 fallback，以及 contract digest。Transcript v2 记录私有媒体快照、上游 artifact hash、内容级 engine/model/adapter 身份、完整 chunk-set、严格时间戳、确定性文本和每个输出文件的 bytes/hash。绝对路径仍作为本机复验元数据保存，但不进入稳定 engine/job digest；相同内容复制到另一条安全本地路径不会产生新的内容身份。

下游不得仅检查 `status: complete`：转写必须通过显式 `--source-artifact` 消费 URL 下载结果，并重验媒体；ingest 必须严格复验 transcript 的 schema 与语义，但默认不访问可能已经删除的源媒体或伴随输出。

构建 receipt 保存在 `<vault>/.awesome-capture/vault-build.json`；ingest receipt 保存在 `<vault>/.awesome-capture/receipts/<完整稳定 ID>.json`。receipt 是事务的最后提交标记，旧版、伪造、路径不匹配或身份不一致的 receipt 都是冲突，不能作为复用依据。

## 稳定性与安全原则

- 每个 skill 只负责一个核心目标。
- 下载成功必须经过真实媒体流检查，缩略图、metadata 和残片不算成功。
- URL hostname 使用精确后缀白名单，不使用字符串包含判断。
- 下载 manifest 不保留 Cookie、签名查询参数、API key 或私有 header。
- 原始 extractor `.info.json` 只可能暂存在 `0700` 私有 staging 或受管 quarantine，可能包含签名 CDN URL 或请求 metadata；不要共享或提交到 Git。完成目录只发布 `0600` 的脱敏 `source.info.json`。
- transcript、artifact、receipt 和知识笔记可能包含绝对路径及原始内容，默认都应视作用户数据。
- 下载与转写把外部工具限制在 `<output>/.awesome-capture-media/v2` 下的 `0700` 私有 staging；子进程工作目录由已持有的目录 FD 固定，媒体输入尽可能通过继承的只读 FD 交接。媒体、state、journal、lock 和 receipt 使用 `0600`。
- 所有受管路径拒绝 symlink、hardlink、父路径穿越和非普通文件；授权判断使用 POSIX no-follow/dir-fd 操作，不依赖字符串形式的 `resolve()`。
- 下载、转写、build 与 ingest 使用持久锁和可恢复 journal；artifact 或 receipt 最后发布。进程崩溃后先运行对应 `recover`，冲突数据不会被自动覆盖或删除。
- 读取浏览器 Cookie 前必须获得用户对指定浏览器或 Cookie 文件的明确授权。
- 转写不会把标题、简介、章节摘要或模型补写冒充原始语音。
- 中断续跑绑定来源、引擎、模型、adapter、设置和完整 chunk-set hash；身份变化后不得混用旧状态。
- external adapter 是用户显式信任并执行的本地代码；必须传 `--trust-external-adapter`，这不等同于操作系统级网络沙箱。
- 知识库写入使用预览、无覆盖和幂等 receipt。
- 拒绝文件系统根目录、用户 home、`.obsidian` 本身、路径穿越和受管目录 symlink。
- 建库不安装社区插件，不修改 Obsidian 全局 vault 注册表，不依赖未公开的 `.obsidian/*.json`。
- 平台风控、登录、会员、私密、地区限制和 DRM 是显式边界，不以无限重试伪装为稳定。

## 验证、CI 与发布证据

仓库提供两类互补验证：

- `.github/workflows/tests.yml` 配置 Ubuntu Python 3.11–3.14 和 macOS
  Python 3.11、3.14 的离线 no-skip 测试。每个 job 都会检查
  ffmpeg/ffprobe、canonical/vendored contract 一致性、JSON Schema、完整测试图、
  repository hygiene 和 `git diff --check`。
- `.github/workflows/smoke.yml` 提供只接受预登记 case alias 的手动真实 smoke。
  GitHub workflow 只接受原仓库默认分支，并绑定
  `awesome-capture-smoke` Environment；维护者必须在仓库设置中配置
  required reviewers 后才可把它作为受控发布环境。下载 case 使用公开样本；
  每个样本都绑定 registry 中预登记的脱敏 URL SHA-256 指纹，harness 会在联网
  下载前复验规范化 URL 与指纹；
  ASR case 只在受保护 runner 上使用预置本地模型，不在工作流中下载模型或
  接收任意 URL、Cookie 和浏览器路径。receipt 通过独立的 schema、digest、
  case、单文件、脱敏和 outcome 复验后才上传；每次 workflow attempt 使用
  唯一 receipt 目录。

已登记的下载 smoke 组合覆盖五个平台及其实际受支持路由：YouTube、Bilibili、X
匿名下载，Douyin 隔离临时浏览器，以及 TikTok/X 的 gallery fallback。
其中 `twitter-anonymous` 使用真实 `yt-dlp` 直连，证明 X 的自然匿名路径。
`tiktok-gallery-fallback` 与 `twitter-gallery-fallback` 各自绑定不可互换的
registry 固定 fault profile：对各自预登记公开样本注入一次、在 receipt 中
明确披露的 `yt-dlp` `NETWORK_ERROR`，随后由未修改的生产 fallback gate
选择真实 `gallery-dl` 完成获取。两项 case 都只证明回退韧性，不表示 TikTok
或 X 在该次运行中自然失败。
X/Twitter 的 yt-dlp 与 gallery-dl 获取路径会使用官方 `--force-ipv4` 选项，
以规避已复现的媒体 CDN TLS EOF；证书校验仍保持开启，其他平台不受影响。

这些受控故障不是通用测试后门：workflow 只接收 case alias，registry 只允许
case、平台和 fault profile 的固定绑定；TikTok 与 X 的 profile 不能互换，也
不存在调用者可传入的任意 fault CLI、workflow 或环境变量输入。

离线测试证明契约、安全边界、故障恢复和幂等行为，不证明外部平台或具体
ASR/硬件组合当前可用。对外发布受影响的平台或引擎时，应生成
`outcome: pass` 且匹配当前 `implementation_digest` 的正式脱敏 smoke
receipt。receipt 时间仅作为审计记录，不设固定过期门槛；缺少匹配当前实现
的 receipt 时，不应把旧实现或历史运行结果当作当前发布证据。
正式 Release 读取候选 commit 中已审查的 `smoke/release-scope.json`，只要求
受影响组件映射出的预登记 case 提供当前 passing receipt；不会仅因仓库登记了
其他平台或 ASR 引擎就要求全部重跑。Scope 使用
`download`、`download:<platform>`、`transcription` 或
`transcription:<engine>`；确无外部执行路径变化时必须显式声明
`external_impact: none`，不能在 workflow dispatch 时临时缩小范围。目录内
所有 receipt 仍会逐份通过严格 schema、语义、脱敏、已注册 case 和 outcome
校验。Scope 还绑定上一 release 的版本与 commit；门禁会比较该基线到候选
`HEAD` 的执行脚本、skill 行为规范、case registry 和 contract 变化，声明
只能扩大机器推导的最低组件集合，不能缩小。正式发布时，基线版本必须严格
低于候选版本、恰好是 changelog 中紧邻的上一版本，并由对应的不可移动轻量
tag 精确指向；在自动发布流程建立前记录的 `0.1.0` 历史版本边界从未创建
tag/Release，仅允许仓库中写死的完整 commit SHA 作为一次性 bootstrap
边界。普通 PR CI 仍允许真实 receipt 目录为空。

本地执行与 CI 相同的核心门禁：

```bash
python3 tools/sync_vendored.py --check
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip
PYTHONDONTWRITEBYTECODE=1 python3 tools/check_repository_hygiene.py
git diff --check
```

真实平台与 ASR smoke 不进入普通 PR 的联网门禁。先用
`python3 tools/run_smoke.py list` 查看预登记 alias，再以
`python3 tools/run_smoke.py run <alias> --receipt-dir <私有目录>` 运行与
手动 workflow 同源的 harness。可运行 `python3 tools/smoke_receipts.py digest`
计算实现身份，并用
`validate ... --require-pass --require-current-digest`
校验生成的脱敏 receipt。`smoke/cases.json` 登记 case alias、证据要求、秘密
环境变量名和下载样本的脱敏 SHA-256 指纹，但不保存原始 URL；受控 TikTok/X
fallback 还分别固定登记不可互换的 fault profile。receipt 禁止原始 URL、
Cookie、token、媒体内容、transcript 和私有绝对路径。

维护者可用下面的命令查看精确映射并复验正式候选：

```bash
python3 tools/smoke_receipts.py components
python3 tools/smoke_receipts.py validate-release
```

正式 smoke receipt 也只证明其记录的 commit、implementation digest、工具版本和公开样本上的链路可运行，不证明所有账号、地区、网络或未来平台版本都可用。

## 项目结构

```text
.
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md
│   └── workflows/
│       ├── tests.yml
│       ├── smoke.yml
│       └── release.yml
├── AGENTS.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── contracts/          # canonical schemas、runtime、fixtures 与 manifest
├── LICENSE
├── README.md
├── RELEASING.md
├── requirements-ci.lock
├── SECURITY.md
├── smoke/              # 公开 case、release scope 与脱敏 receipt；不存媒体或秘密
├── skills/
│   ├── download-video/
│   ├── transcribe-media/
│   ├── ingest-knowledge/
│   └── build-obsidian-vault/
├── tests/
├── VERSION              # 仓库发行版本的唯一源码
├── VERSIONING.md
└── tools/
    ├── check_repository_hygiene.py
    ├── release.py
    ├── run_smoke.py
    ├── run_tests.py
    ├── smoke_receipts.py
    └── sync_vendored.py
```

每个 skill 目录遵循相同结构：

```text
<skill>/
├── VERSION             # 与根 VERSION 同步的 standalone 版本
├── SKILL.md            # Agent 必须完整阅读的规范
├── agents/openai.yaml  # UI 元数据
├── scripts/            # 确定性实现；含生成的 _contracts/ standalone bundle
├── references/         # 按 SKILL.md 路由读取的深入说明
└── assets/             # 可选模板或示例
```

## 扩展规则

新增能力时优先增加新 skill，例如批量下载、OCR、网页抓取、文档解析或事实核查。

只有当新能力与现有 skill 拥有相同输入、成功判据和副作用时，才扩展现有 skill。新增或修改正式契约时必须：

1. 先定义生产者与消费者；
2. 在 `contracts/schemas/` 修改 canonical schema，必要时升级版本并明确 breaking policy；
3. 运行 `tools/sync_vendored.py --apply`，不得手改任一 `_contracts/` 副本；
4. 同步更新生产者、每个消费者、正反 fixtures，以及成功、失败、冲突、恢复和幂等测试；
5. 用真实代表性样本验证外部工具，并生成不含秘密的 smoke receipt；
6. 更新 `AGENTS.md`、目标 `SKILL.md` 和相关 reference；
7. 不把平台登录绕过、远程上传或知识库写入作为隐式副作用。

## 贡献

所有普通改动都应从独立分支通过 Pull Request 合并，不把直接 push 到
`main` 当作正常工作流。PR 的标题、证据矩阵、CI 失败处理、安全公开边界和
合并规则见 [`CONTRIBUTING.md`](CONTRIBUTING.md)；提交时请使用仓库提供的
[PR 模板](.github/PULL_REQUEST_TEMPLATE.md)。

面向使用者的变化应先写入
[`CHANGELOG.md`](CHANGELOG.md) 的 `[Unreleased]`；版本升级规则、breaking
change 要求和 release/schema/digest 的区别见
[`VERSIONING.md`](VERSIONING.md)。维护者发布前还应遵循
[`RELEASING.md`](RELEASING.md)。

提交改动前至少运行：

```bash
python3 tools/release.py check
python3 tools/sync_vendored.py --check
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip
PYTHONDONTWRITEBYTECODE=1 python3 tools/check_repository_hygiene.py
git diff --check
```

涉及平台 extractor、ASR 引擎或外部工具版本时，单元测试不足以证明稳定；还必须对受影响的平台或代表性音频完成真实 smoke test，并保存符合 `awesome-capture.smoke-receipt/v1` 的脱敏证据。发布时，受影响路径应有匹配当前 implementation digest 的 passing receipt；时间戳保留用于审计，但不会仅因年龄而使 receipt 失效。

## 许可证

本项目使用 [MIT License](LICENSE)。

MIT 只覆盖本仓库的原创源码和文档。`yt-dlp`、FFmpeg、Deno、Playwright、`gallery-dl`、Whisper 引擎、模型权重及用户下载内容保留各自许可证；本仓库不捆绑这些第三方二进制或模型。若未来发布容器、安装包或预编译二进制，需要重新进行许可证审计。

安全与隐私问题的报告方式见 [SECURITY.md](SECURITY.md)。
