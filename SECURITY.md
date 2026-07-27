# Security Policy

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

## 支持边界

本项目不会：

- 绕过 DRM、付费、私密、地区或登录限制；
- 在未授权时读取浏览器 Cookie；
- 静默上传本地媒体到远程 ASR；
- 覆盖不匹配的 Obsidian 文件；
- 修改 Obsidian 全局 vault 注册表或未公开设置。

若发现实现违反这些边界，应按安全问题处理。
