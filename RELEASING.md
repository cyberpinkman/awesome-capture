# Release Process

本文描述 Awesome Capture 的公开发布流程。发布使用整个仓库统一的 SemVer，
由人工触发，tag 与 GitHub Release 一经创建即视为不可变。

## 1. 准备版本

0. 发行准备也必须通过符合 [`CONTRIBUTING.md`](CONTRIBUTING.md) 的独立
   Pull Request 进入 `main`；不要直接在 `main` 上试错或修改版本。
1. 根据 [`VERSIONING.md`](VERSIONING.md) 选择版本号。
2. 将 [`CHANGELOG.md`](CHANGELOG.md) 中 `[Unreleased]` 的用户可见变化移动
   到 `## [X.Y.Z] - YYYY-MM-DD`，保留空的 `[Unreleased]`。
3. 更新根目录 `VERSION`，再同步四个 `skills/*/VERSION`。版本文件不包含
   `v`；tag 包含 `v`。
4. 若 README 的稳定安装示例或版本链接需要变化，一并更新。
5. 确认仓库中没有媒体、模型、Cookie、artifact/state、私有 receipt、日志、
   本机路径或内部实施材料。

版本元数据使用标准库工具维护：

```bash
python3 tools/release.py sync
python3 tools/release.py check
python3 tools/release.py check-release --requested-version X.Y.Z
python3 tools/release.py notes --output "/new/path/release-notes.md"
```

`sync` 只从根 `VERSION` 生成四个 skill 副本；`check` 校验 SemVer、版本同步和
changelog 结构；`check-release` 还要求目标版本与 changelog 一致且
`[Unreleased]` 为空。`notes` 从对应 changelog 小节生成正文，不包含版本 H2、
其他版本或底部比较链接；输出路径必须尚不存在，工具不会覆盖文件。所有命令
成功时 stdout 恰好输出一个 JSON object；release notes 以文件路径交接，不
手工维护第二份内容。

## 2. 运行发布门禁

在干净工作树中运行：

```bash
python3 tools/release.py check
python3 tools/sync_vendored.py --check
PYTHONDONTWRITEBYTECODE=1 python3 tools/run_tests.py --fail-on-skip
PYTHONDONTWRITEBYTECODE=1 python3 tools/check_repository_hygiene.py
git diff --check
```

涉及外部平台、ASR 引擎或其执行路径的变化，还必须提供
`awesome-capture.smoke-receipt/v1` 的正式脱敏证据。Receipt 必须通过 schema、
digest、脱敏和 `outcome: pass` 校验，并匹配待发布实现；时间戳保留用于审计，
不设置固定过期天数。不得用旧 implementation digest 的结果替代当前实现证据。

正式 Release 当前要求 `smoke/cases.json` 中每个预登记 case 都有匹配证据。
从 `manual smoke` workflow 下载 receipt artifact 后，应先逐份运行严格校验，
再仅把通过校验的 JSON 保存为 `smoke/receipts/<case-id>.json` 并提交。普通 PR
CI 可以在该目录为空时通过；Release workflow 会额外传入
`--require-all-cases`，零份或部分 receipt 都会失败关闭。

发布 commit 必须位于 `main`，并且该确切 commit SHA 的完整 tests workflow
成功。不要仅依赖较早 commit、其他分支或部分 matrix job 的绿灯。

## 3. 创建 GitHub Release

从 GitHub Actions 手动运行 `release` workflow，输入不带 `v` 的 `X.Y.Z`。
工作流会重新执行发布元数据、契约同步、仓库卫生和 smoke receipt 门禁，
再确认运行中的 commit 仍是远端 `main` HEAD，并且同一 SHA 的主测试工作流
成功。

远端预检和发布使用以下固定规则：

1. Tag 与 release 都不存在：创建指向确切 release commit 的 `vX.Y.Z` tag，
   再以 changelog notes 创建稳定 GitHub Release。
2. 轻量 tag 已存在、指向同一 commit 且 release 缺失：复验 tag 后补建
   release，不移动 tag。
3. Tag 与 release 都存在，且 SHA、稳定 release 元数据、标题和 changelog
   notes 完全一致：返回 `reused`，不修改远端对象。
4. Release 缺少预期 tag、tag 不是指向确切 commit 的轻量 tag，或者已有
   release 是 draft/prerelease、标题或 notes 不一致：返回明确冲突。

工作流最后会重新读取远端 tag 与 release，验证目标 SHA、tag、版本和 notes。
流程不得移动 tag、覆盖 release 或先删除冲突对象。

Release 不上传媒体、模型、Cookie、浏览器资料、原始日志、私有路径、用户
artifact/state/vault 或未经正式脱敏校验的 receipt。默认发布物是 GitHub 为
不可变 tag 生成的源码归档和公开 release notes。

## 4. 发布后核验

- 确认 [Releases](https://github.com/cyberpinkman/awesome-capture/releases)
  页面显示 `vX.Y.Z`，并且 tag 指向预期 commit SHA。
- 从 release 源码归档或全新 clone 中运行 `python3 tools/release.py check`。
- 确认 README 的稳定安装命令能检出该 tag，四个 skill 的 `VERSION` 一致。
- 确认 release notes 只包含该版本 changelog，不包含 `[Unreleased]`、其他
  版本或链接 footer。

如果发布后发现问题，不修改或删除既有 tag/release。修复代码和 changelog，
升级到新的 patch（若含 breaking change 则按版本策略升级），重新走完整流程。
