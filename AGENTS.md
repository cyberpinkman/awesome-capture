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
2. 浏览 `README.md` 的“四个 Skills”“Agent 层与脚本层”“文件契约”。
3. 根据任务路由，完整阅读目标 `skills/<name>/SKILL.md`。
4. 只读取该 `SKILL.md` 针对当前情形明确引用的 references。
5. 在执行外部工具或写文件前，先运行对应的 `doctor`、`inspect`、`validate` 或 `plan`。

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
   - `status == "complete"`
   - `media.has_video == true`
   - `media.path` 存在
3. 读取 artifact 的 `media.path`，不要猜下载文件名。
4. 把该本地路径交给 `transcribe-media`。
5. 要求 transcript artifact：
   - `artifact_type == "transcript"`
   - `status == "complete"`
   - source hash 存在
   - timestamps 单调且不越界
6. 若有非空语音，询问一次：“是否需要把这份内容写入本地 Obsidian 知识库？”

`transcribe_media.py` 直接收到 URL 时会拒绝并提示使用 download skill；这是模块边界，不是缺陷。

### Transcript → Obsidian

1. 解析现有 vault 路径；没有路径时询问，不要扫描整个 home。
2. 用 `knowledge_writer.py validate-transcript` 校验证据。
3. 完整阅读 transcript。
4. 按 `skills/ingest-knowledge/references/note-schema.md` 在 vault 外起草 Markdown：
   - 恰好一个 H1；
   - 至少两个 H2；
   - 重要结论带时间戳；
   - 推断明确标为推断；
   - 缺少证据的内容放入“待验证”。
5. 先执行 `commit ... --dry-run`。
6. 用户已明确授权后，移除 `--dry-run`。
7. 重复写入必须返回 `reused`；不得制造重复笔记。

`knowledge_writer.py` 不负责生成摘要或观点；Agent 负责基于完整证据起草。

### Interview → Vault

1. 依次访谈目的/产出、输入/检索、维护成本、命名/链接/附件/同步和目标路径。
2. 读取 `profiles.md`，选择最接近的最小 profile。
3. 在 vault 外生成 `awesome-capture.vault-config/v1` JSON。
4. `validate-config`。
5. `plan`，向用户展示路径、文件夹、模板、链接风格和冲突。
6. 获得确认后 `build --apply`。
7. `audit`。

`vault_builder.py` 不执行访谈，也不启动 Obsidian。

## 4. Contract Map

### A. `awesome-capture.artifact/v1`

由下载和转写阶段使用。通过 `artifact_type` 与字段区分 video 和 transcript。

- Video 交接核心：`status`、`media.path`、`media.sha256`、`media.has_video`。
- Transcript 交接核心：`status`、`source.sha256`、`transcription.engine_identity`、`segments[]`、`text`。
- 下游读取 artifact 中的绝对路径；不得通过目录扫描或文件名规则猜测。
- Cookie 值、API key、私有 header 和签名查询参数不得进入 artifact。

完整 transcript 规则：`skills/transcribe-media/references/artifact-contract.md`。

### B. `awesome-capture.vault-config/v1`

用于建库，不是媒体 artifact。配置 schema：

`skills/build-obsidian-vault/references/config-schema.md`

构建回执：

`<vault>/.awesome-capture/vault-build.json`

### C. Ingest Receipt

用于知识入库幂等性，不是 artifact 或 vault config：

`<vault>/.awesome-capture/receipts/<stable-id>.json`

相同来源和 schema 应复用 receipt。存在路径但 receipt 不匹配时必须报冲突。

## 5. Repository-root Commands

所有命令默认从仓库根目录运行。

```bash
# 全套离线测试
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

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

# Vault 配置检查和预览
python3 skills/build-obsidian-vault/scripts/vault_builder.py validate-config \
  "/absolute/path/to/config.json"
python3 skills/build-obsidian-vault/scripts/vault_builder.py plan \
  "/absolute/path/to/config.json" \
  --vault "/absolute/path/to/vault"

# Transcript 证据检查
python3 skills/ingest-knowledge/scripts/knowledge_writer.py validate-transcript \
  "/absolute/path/to/transcript.json"
```

skill 自己的 `SKILL.md` 中使用 `python3 scripts/...`，那是以 skill 目录为当前工作目录的写法。

## 6. Non-negotiable Invariants

### Download

- 仅接受 Douyin、TikTok、Bilibili、YouTube、X/Twitter 的精确 host 后缀。
- 默认单条视频；不得隐式展开 playlist。
- `yt-dlp --ignore-config` 保持启用。
- 缩略图、metadata、空文件、无视频流文件不算成功。
- 匿名失败后只允许平台规定的有限回退；不得无限重试。
- 不绕过 DRM、付费、私密、地区或授权限制。

### Transcription

- 本地媒体不得被静默上传到远程服务。
- 不下载模型；模型由用户提供并记录 hash。
- 标题、简介和 LLM 重建不属于语音转写。
- 续跑状态必须绑定来源、引擎、模型、设置和 chunk hash。
- whisper.cpp GPU 失败必须隔离，并允许 CPU 回退；不得把崩溃当空转写。

### Vault and Ingest

- 先 preview/dry-run，再写入。
- 不覆盖不同内容。
- 拒绝 root、home、`.obsidian`、父路径穿越和受管目录 symlink。
- 不写 Obsidian 全局注册表，不启用 Sync，不安装社区插件。
- 普通 Markdown/YAML 和 skill 自有 receipt 是持久化边界。

## 7. Definition of Done

### 修改代码

- 目标行为有测试；
- `python3 -m unittest discover -s tests -v` 全部通过；
- 未产生 `__pycache__`、模型、媒体、Cookie 或临时输出；
- 修改了契约时同步更新生产者、消费者、测试、`SKILL.md` 和 reference；
- 外部平台/引擎变化有真实 smoke test，不能只靠 mock。

### 下载任务

- 返回绝对媒体路径和 artifact 路径；
- artifact 为 complete；
- `ffprobe` 验证视频流；
- 报告实际 auth mode、fallback 和 warnings。

### 转写任务

- 返回完整 transcript artifact 和所有文本/字幕路径；
- 报告引擎、语言、时长、segment 数和 GPU fallback；
- 非空语音才询问是否入库。

### 入库任务

- 返回知识笔记、原始转写和 receipt 的绝对路径；
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
4. 实现确定性 script；
5. 编写正向、负向、冲突、恢复和安全测试；
6. 更新本文件的路由与契约图；
7. 用真实代表性输入验证；
8. 明确尚未支持的边界。

推荐后续独立 skills：`download-batch`、`extract-webpage`、`extract-document`、`run-ocr`、`fact-check-content`。

## 9. Known Tested Baseline

截至 2026-07-27：

- Python 3.9、3.13、3.14 的完整仓库测试均为 22/22；
- `yt-dlp 2026.07.04`；
- FFmpeg/FFprobe 8.1；
- Deno 2.9.4；
- `gallery-dl 1.32.8`；
- Playwright 1.60.0 + 隔离 Chromium；
- `whisper.cpp 1.9.1`。

版本号是可复现基线，不是永久上限。升级下载器或 ASR 后必须重新做代表性真实测试。
