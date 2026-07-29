# Changelog

本文件记录 Awesome Capture 面向使用者的公开变化。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 建立公开 Pull Request 贡献规范、证据矩阵和 PR 模板，明确分支工作流、
  contract/smoke/安全要求、CI 失败处理及最终 head SHA 合并门槛。

### Security

- 手动真实 smoke 仅允许原仓库默认分支进入受控 Environment；持久
  self-hosted runner 不执行 fork/PR ref，receipt 仅在独立通过 schema、
  current digest、case、单文件、脱敏和 outcome 复验后上传，每次 attempt
  使用唯一目录隔离历史结果。

## [0.1.0] - 2026-07-28

### Added

- 提供四个可独立复制运行、也可通过版本化 artifact 组合的 skills：
  `download-video`、`transcribe-media`、`ingest-knowledge` 和
  `build-obsidian-vault`。
- 支持下载单条 Douyin、TikTok、Bilibili、YouTube 和 X/Twitter 公开视频，
  并在发布结果前执行 hash 与 `ffprobe` 媒体流验证。
- 支持 whisper.cpp、faster-whisper、MLX Whisper 和显式可信 external
  adapter 的本地 ASR，输出带时间戳的 JSON、Markdown、TXT、SRT 和 VTT。
- 提供 Obsidian vault 的访谈配置、稳定 plan、无覆盖构建、知识入库、幂等
  receipt 与只读 audit。
- 建立 canonical JSON Schema Draft 2020-12 契约、标准库语义验证器及每个
  standalone skill 自带的生成副本。
- 提供 Linux/macOS、Python 3.11–3.14 的 no-skip CI，以及只接受预登记公开
  case alias 的脱敏 smoke receipt 流程。

### Changed

- URL 转写使用明确的 video artifact 路径交接；转写消费者会重新验证
  contract digest、媒体 hash 和流证据，不再猜测相邻文件名。
- 所有 ASR 引擎均要求显式本地模型，并使用模型、binary 和 adapter 的内容
  hash 建立任务身份；不自动下载模型，也不隐式切换远程服务。
- 本地媒体会先复制为私有、已哈希快照，再参与 chunk、续跑和发布流程。
- Vault build 与 ingest 均要求先取得稳定 `plan_sha256`，写入时在独占锁内
  重新计算并拒绝过期计划。
- 完整安全保证的支持范围明确为 Python 3.11–3.14 的 macOS/Linux POSIX
  环境；缺少必需文件系统能力时失败关闭。

### Fixed

- Chunk manifest 现在拒绝缺失、额外、断号、时间线不连续或内容被替换的
  chunk，缺失结果不会再被解释为空语音。
- 下载、转写、建库和入库增加持久锁、事务 journal、最后提交标记与显式
  recover，支持在已验证边界内恢复进程崩溃。
- Vault build 与 ingest 的跨日复跑和并发请求使用稳定内容身份；相同请求
  复用结果，不同内容返回明确冲突。
- Vault 首次建库并发时会区分内部持久锁与用户已有内容，避免因进程调度误报
  `EXISTING_VAULT_REQUIRES_OPT_IN`。
- Release 门禁会拒绝零份或仅覆盖部分预登记 case 的 smoke 证据，避免把普通
  PR 的“允许无真实 smoke”规则误用于正式发布。
- 下载 smoke 按平台实际路由登记：YouTube/Bilibili/X 匿名、Douyin 隔离
  临时浏览器、TikTok/X gallery fallback；fallback case 同时验证匿名首试
  和受控回退，不要求已被平台强制回退的路径伪造匿名成功。
- whisper.cpp GPU 失败与 CPU 回退相互隔离；GPU 或 CPU 崩溃不会被记录为
  空转写。

### Security

- 受管路径改用 POSIX no-follow、dir-fd 权限边界，拒绝路径穿越、symlink、
  hardlink、特殊文件、不安全所有权和不安全权限。
- 下载器和 ASR 子进程只能在私有 staging 内写入；媒体、state、journal、
  lock、artifact 和 receipt 使用受限权限，完成标记最后发布。
- CLI 错误、artifact 与 smoke receipt 执行秘密扫描和脱敏，禁止保存 Cookie、
  header、token、签名 URL、原始 extractor JSON 或私有日志。
- Producer 与 consumer 分别复验 canonical contract digest、结构、跨字段
  语义和已获授权的文件证据。

### Breaking

- 媒体交接只接受 `awesome-capture.artifact/v2`。artifact/v1、无版本
  state/receipt 和未知版本会被直接拒绝，不提供双读或静默迁移。
- URL 工作流必须把下载结果以显式 `--source-artifact PATH` 传给
  `transcribe-media`；转写脚本本身不接受 URL。
- `--engine auto` 只会在同时提供本地 whisper.cpp binary 与模型时选择
  whisper.cpp；faster-whisper、MLX Whisper 和 external adapter 不会被自动
  选择。
- External ASR 必须同时提供 `--adapter`、`--model` 和
  `--trust-external-adapter`，stdout 必须是一个严格的
  `awesome-capture.external-asr/v1` JSON object。
- Vault 的正式写入必须回传 preview/dry-run 生成的
  `--expected-plan-sha256`。

### Migration

- 本版本不提供旧 artifact、state 或 receipt 的原地迁移。请先备份旧输出和
  vault，再将 `v0.1.0` 的四个 skills 作为同一版本安装。
- 在新的空输出目录中重新下载或转写，以生成 v2 artifact 和 v1 state；
  URL 工作流须显式传递下载 artifact。
- Vault config 仍使用 `awesome-capture.vault-config/v1`。发现 legacy
  receipt 时工具会阻止复用或覆盖；请人工审阅后选择新的 vault/目标路径，
  或按自己的备份与数据保留策略处理旧数据。

[Unreleased]: https://github.com/cyberpinkman/awesome-capture/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cyberpinkman/awesome-capture/releases/tag/v0.1.0
