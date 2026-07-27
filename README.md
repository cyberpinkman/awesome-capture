# Awesome Capture

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一组面向本地 AI Agent 的模块化信息捕获 skills：下载公开视频、转写音视频、把内容结构化写入 Obsidian，以及按用户习惯搭建 Obsidian 知识库。

> 当前实测环境：Apple Silicon macOS，Python 3.9–3.14。
> 当前能力边界：单个公开视频、本地音视频、本地 Obsidian vault；不支持 DRM、付费、私密内容或登录绕过。

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
| [`download-video`](skills/download-video/SKILL.md) | 识别并下载单条 Douyin、TikTok、Bilibili、YouTube、X/Twitter 视频 | 可播放媒体、来源 metadata、已验证 video artifact |
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

### 1. 克隆

```bash
git clone https://github.com/cyberpinkman/awesome-capture.git
cd awesome-capture
```

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

所有内置 Python 脚本只使用标准库，要求 Python 3.9 或更高版本。外部媒体能力按需安装：

| 能力 | 必需依赖 | 可选增强 |
|---|---|---|
| 视频下载 | `yt-dlp >= 2026.07.04`、`ffmpeg`、`ffprobe` | Deno、`gallery-dl >= 1.32.8`、Playwright Chromium、`curl-cffi` |
| 本地转写 | `ffmpeg`、`ffprobe`，以及一个明确选择的 ASR 引擎 | `whisper.cpp` + 本地 GGML 模型、`faster-whisper`、显式外部 adapter |
| Obsidian 入库 | Python 3.9+、一个现有本地 vault | 无 |
| Obsidian 建库 | Python 3.9+ | Obsidian 桌面应用只用于用户之后打开 vault |

本项目不会自动下载模型，不会自动读取个人浏览器配置，也不会静默切换到远程转写服务。

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

### 检测和下载

```bash
python3 skills/download-video/scripts/download_video.py detect "<video-url>"

python3 skills/download-video/scripts/download_video.py download "<video-url>" \
  --output-dir "/absolute/output/directory"
```

下载成功必须同时满足：

- 进程退出码为 0；
- artifact 的 `status` 为 `complete`；
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
  --engine auto \
  --model "/absolute/path/to/ggml-model.bin" \
  --whisper-cpp-bin "/absolute/path/to/whisper-cli"
```

URL 输入由 Agent 编排：先下载，读取 video artifact 的 `media.path`，再把该本地路径传给转写脚本。

### 建立 Obsidian Vault

```bash
python3 skills/build-obsidian-vault/scripts/vault_builder.py validate-config \
  "/absolute/path/to/vault-config.json"

python3 skills/build-obsidian-vault/scripts/vault_builder.py plan \
  "/absolute/path/to/vault-config.json" \
  --vault "/absolute/path/to/new-vault"

python3 skills/build-obsidian-vault/scripts/vault_builder.py build \
  "/absolute/path/to/vault-config.json" \
  --vault "/absolute/path/to/new-vault" \
  --apply

python3 skills/build-obsidian-vault/scripts/vault_builder.py audit \
  --vault "/absolute/path/to/new-vault"
```

必须在向用户展示 `plan` 并获得确认后才能 `--apply`。

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
```

确认 dry-run 结果后，移除 `--dry-run` 才会写入。相同来源再次写入时应返回 `result: reused`，而不是创建重复笔记。

## 文件契约

项目包含三类不同契约，不应混称或互相猜测。

### 1. Video / Transcript Artifact

版本：`awesome-capture.artifact/v1`

下载 artifact 至少提供：

- `artifact_type`、`status`；
- 原始 URL 和平台；
- `media.path`、`media.sha256`、媒体探测结果；
- 下载引擎、版本、授权模式和不含 Cookie 值的 warnings。

转写 artifact 至少提供：

- `artifact_type: transcript`、`status: complete`；
- `source.path`、`source.sha256`、时长；
- ASR 引擎与模型身份；
- 单调、不越界的 `segments[]`；
- `text` 以及 Markdown、TXT、SRT、VTT、state 路径。

完整约束见 [`artifact-contract.md`](skills/transcribe-media/references/artifact-contract.md)。下游必须读取 artifact，不能猜测上游文件名或目录。

### 2. Vault Config 与构建 Receipt

- 配置版本：`awesome-capture.vault-config/v1`
- 示例：[`vault-config.example.json`](skills/build-obsidian-vault/assets/vault-config.example.json)
- schema 说明：[`config-schema.md`](skills/build-obsidian-vault/references/config-schema.md)
- 构建 receipt：`<vault>/.awesome-capture/vault-build.json`

### 3. Ingest Idempotency Receipt

- 位置：`<vault>/.awesome-capture/receipts/<stable-id>.json`
- 身份由来源 hash 和 schema 版本生成；
- 已存在且校验一致时返回 `reused`；
- 路径冲突但没有匹配 receipt 时必须失败，不得覆盖。

## 稳定性与安全原则

- 每个 skill 只负责一个核心目标。
- 下载成功必须经过真实媒体流检查，缩略图、metadata 和残片不算成功。
- URL hostname 使用精确后缀白名单，不使用字符串包含判断。
- 下载 manifest 不保留 Cookie、签名查询参数、API key 或私有 header。
- 原始 `.info.json` 是权限为 `0600` 的本地取证文件，可能包含签名 CDN URL 或请求 metadata；不要共享或提交到 Git。
- transcript、artifact、receipt 和知识笔记可能包含绝对路径及原始内容，默认都应视作用户数据。
- 读取浏览器 Cookie 前必须获得用户对指定浏览器或 Cookie 文件的明确授权。
- 转写不会把标题、简介、章节摘要或模型补写冒充原始语音。
- 中断续跑绑定来源、引擎、模型、设置和分块 hash；身份变化后不得混用旧状态。
- 知识库写入使用预览、无覆盖和幂等 receipt。
- 拒绝文件系统根目录、用户 home、`.obsidian` 本身、路径穿越和受管目录 symlink。
- 建库不安装社区插件，不修改 Obsidian 全局 vault 注册表，不依赖未公开的 `.obsidian/*.json`。
- 平台风控、登录、会员、私密、地区限制和 DRM 是显式边界，不以无限重试伪装为稳定。

## 已验证结果

截至 2026-07-27：

- 仓库回归测试与公开结构检查：22/22；
- GitHub Actions 会在 Python 3.9、3.13、3.14 上运行同一套离线测试；
- 四个 skill 均通过结构校验；
- `yt-dlp 2026.07.04` 已实际下载并经 `ffprobe` 验证 Douyin、TikTok、Bilibili、YouTube、X/Twitter 的公开样本；
- Douyin 隔离 Chromium 临时会话完成真实回退下载，未读取个人浏览器配置；
- TikTok 的 `yt-dlp → gallery-dl` 回退已用确定失败的主引擎测试替身触发并真实下载；
- `whisper.cpp 1.9.1` 完成中文样本转写，Metal 失败后 CPU 回退成功；
- 完成“转写 → 建库 → dry-run → 原子入库 → 审计 → 幂等复用”端到端测试；
- 建库冲突、非法 transcript、host spoof、签名参数泄漏和 symlink 逃逸均有负向测试。

运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

这些结果证明当前测试环境和给定公开样本上的链路可运行，不证明所有账号、地区、网络或未来平台版本都可用。

## 项目结构

```text
.
├── .github/workflows/tests.yml
├── AGENTS.md
├── LICENSE
├── README.md
├── SECURITY.md
├── skills/
│   ├── download-video/
│   ├── transcribe-media/
│   ├── ingest-knowledge/
│   └── build-obsidian-vault/
└── tests/
    ├── test_repository.py
    └── test_skills.py
```

每个 skill 目录遵循相同结构：

```text
<skill>/
├── SKILL.md            # Agent 必须完整阅读的规范
├── agents/openai.yaml  # UI 元数据
├── scripts/            # 确定性实现
├── references/         # 按 SKILL.md 路由读取的深入说明
└── assets/             # 可选模板或示例
```

## 扩展规则

新增能力时优先增加新 skill，例如批量下载、OCR、网页抓取、文档解析或事实核查。

只有当新能力与现有 skill 拥有相同输入、成功判据和副作用时，才扩展现有 skill。新增或修改 artifact 字段时必须：

1. 先定义生产者与消费者；
2. 保持旧字段语义不变，或升级 schema 版本；
3. 添加成功、失败、冲突和幂等测试；
4. 用真实代表性样本验证外部工具；
5. 更新 `AGENTS.md`、目标 `SKILL.md` 和相关 reference；
6. 不把平台登录绕过、远程上传或知识库写入作为隐式副作用。

## 贡献

提交改动前至少运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

涉及平台 extractor、ASR 引擎或外部工具版本时，单元测试不足以证明稳定；还必须对受影响的平台或代表性音频完成真实 smoke test，并记录版本、成功判据和失败边界。

## 许可证

本项目使用 [MIT License](LICENSE)。

MIT 只覆盖本仓库的原创源码和文档。`yt-dlp`、FFmpeg、Deno、Playwright、`gallery-dl`、Whisper 引擎、模型权重及用户下载内容保留各自许可证；本仓库不捆绑这些第三方二进制或模型。若未来发布容器、安装包或预编译二进制，需要重新进行许可证审计。

安全与隐私问题的报告方式见 [SECURITY.md](SECURITY.md)。
