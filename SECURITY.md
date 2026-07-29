# Security Policy

## 支持版本

除具体 release notes 另有说明外，安全修复以
[最新 GitHub Release](https://github.com/cyberpinkman/awesome-capture/releases/latest)
为维护目标。`main` 是未发布开发分支，不应作为稳定版本安装。报告问题时请
同时提供受影响的 release tag；版本兼容与支持规则见
[`VERSIONING.md`](VERSIONING.md)。

## 报告安全问题

请不要在公开 Issue 中提交真实 Cookie、Token、签名 URL、私有媒体、个人文件路径或 Obsidian 内容。

仓库公开后，优先使用 GitHub 的 **Security → Report a vulnerability** 私密报告入口。若该入口不可用，请只创建不含敏感复现数据的 Issue，说明需要私下提供细节。

报告应尽量包含：

- 受影响的 skill 和脚本；
- 可公开的最小复现步骤；
- 预期行为和实际行为；
- 工具与平台版本；
- 是否涉及路径逃逸、覆盖、凭证泄漏或授权绕过。

## 敏感运行产物

以下内容不属于可公开构建产物：

- Cookie 文件、浏览器会话、`.env`；
- yt-dlp 原始 `.info.json`；
- `*.artifact.json`、`state.json` 和 transcript 文件；
- 下载的音视频；
- GGML、GGUF、ONNX、safetensors 等模型；
- Obsidian vault、`.awesome-capture` receipts；
- 包含用户绝对路径的日志或笔记。

仓库的 `.gitignore` 默认排除这些常见文件，但忽略规则不是安全边界。提交前仍应检查暂存清单和敏感字符串。

公开的 `awesome-capture.smoke-receipt/v1` 只能包含 case ID、脱敏来源 fingerprint、工具/模型内容 hash、版本、断言和 implementation digest。不得把原始 URL、Cookie/header/token、媒体或 transcript、原始日志、用户名、hostname 或私有绝对路径写入 smoke receipt。使用 `tools/smoke_receipts.py validate` 在发布前检查 receipt；通过 schema 不代表允许公开底层媒体。

## 安全运行边界

- 完整安全保证仅适用于 Python 3.11–3.14 的 macOS/Linux POSIX 环境。`fcntl`、`dir_fd`、`O_NOFOLLOW`、目录 `fsync`、原子 no-replace rename 或原子 exchange rename 等能力缺失时，写操作必须以 `UNSUPPORTED_PLATFORM` 失败，不能降级为字符串路径检查。
- 受管目录使用 `0700`，媒体、state、journal、lock 和 receipt 使用 `0600`。代码必须拒绝路径穿越、symlink、hardlink、特殊文件、非当前用户拥有的受管对象和不安全权限。
- 下载器与 ASR 子进程只可写入 `<output>/.awesome-capture-media/v2` 的私有 staging。子进程工作目录固定到已打开的目录 FD；媒体输入在适用时通过继承的只读 FD 传递，避免路径交换改变实际输入或输出边界。验证完成后才发布受管输出，artifact/receipt 是最后提交标记。
- 下载、转写、vault build 和 ingest 都使用持久锁与恢复 journal。崩溃后运行对应 `recover`；恢复只能补完 hash 完全匹配的事务，不能自动覆盖或删除冲突数据。
- external ASR adapter 是用户通过 `--trust-external-adapter` 明确授权执行的本地代码。仓库验证其内容身份并最小化环境，但不承诺操作系统级网络沙箱；不可信 adapter 不应执行。

## 契约与供应链完整性

正式 JSON contracts 的唯一源码位于 `contracts/`，各 skill 的 `scripts/_contracts/` 是生成副本。manifest 分离 wire-contract `contract_digest` 与本地安全实现 `runtime_digest`，两组文件都必须逐字节复验。修改后必须运行：

```bash
python3 tools/sync_vendored.py --apply
python3 tools/sync_vendored.py --check
```

当前媒体交接只接受 `awesome-capture.artifact/v2`。artifact/v1、无版本 state/receipt、重复 JSON key、NaN/Infinity、未知字段、未知版本和 producer/consumer contract digest 不一致都必须失败。所有消费者需要自行复验，不能把上游 `status: complete` 当作信任边界。

## 支持边界

本项目不会：

- 绕过 DRM、付费、私密、地区或登录限制；
- 在未授权时读取浏览器 Cookie；
- 静默上传本地媒体到远程 ASR；
- 自动下载或按远程仓库名解析 ASR 模型；
- 自动信任或执行 external adapter；
- 覆盖不匹配的 Obsidian 文件；
- 修改 Obsidian 全局 vault 注册表或未公开设置。

若发现实现违反这些边界，应按安全问题处理。

## CI 与公开验证

普通 PR CI 是离线门禁：校验 canonical/vendored contract 一致性，并通过 `tools/run_tests.py --fail-on-skip` 禁止 skip 和 unexpected success。真实平台 smoke 由受控环境手动执行，结果以脱敏 receipt 作为发布证据，而不是把 Cookie、模型或媒体上传到 CI。

GitHub 的手动 smoke 只允许原仓库默认分支执行，并绑定
`awesome-capture-smoke` Environment；维护者必须为该 Environment 配置人工
审批。持久 self-hosted runner 不执行 fork、PR ref 或任意功能分支代码。
receipt 只有在独立通过 schema、current implementation digest、脱敏和
outcome 复验后才可上传；每次 attempt 使用唯一目录，并要求恰好一个与所选
case 匹配的 receipt。validator 自身失败时不得上传生成的 JSON。
