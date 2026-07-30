# Versioning Policy

Awesome Capture 对整个仓库使用一个锁步的
[Semantic Versioning](https://semver.org/) 版本号。四个 skills、公开文档、
确定性脚本和 canonical contracts 作为一个经过验证的发行单元发布，不分别
维护互相独立的产品版本。

## 版本来源

- 根目录 [`VERSION`](VERSION) 是发行版本的唯一源码，内容不带 `v`，例如
  `0.1.0`。
- 当前发布流程只接受稳定的 `X.Y.Z`，不接受 prerelease 或 build metadata；
  若未来引入预发布通道，须先明确其安装、兼容与 latest-release 规则。
- 每个 `skills/<name>/VERSION` 是随 standalone skill 分发的同版本副本，
  必须与根版本逐字节一致。
- Git tag 和 GitHub Release 使用带 `v` 的形式，例如 `v0.1.0`。
- 已发布 tag 不移动、不覆盖、不删除。发布错误通过更高的新版本修复。
- `main` 是开发分支，可能包含尚未发布或破坏兼容性的变化；普通使用者应
  安装 GitHub Release 对应的 tag。

## 何时升级版本

在 `0.x` 阶段：

- `PATCH`（例如 `0.1.0` → `0.1.1`）：不破坏已记录使用方式的缺陷修复、
  文档修正、安全加固或内部实现改进。
- `MINOR`（例如 `0.1.0` → `0.2.0`）：新增向后兼容的平台、引擎、skill 或
  可选 CLI 能力；`0.x` 阶段无法保持兼容的变化也至少升级 `MINOR`，并在
  changelog 中提供 `Breaking` 与 `Migration`。
- `MAJOR`：项目进入 `1.0.0` 后，任何不兼容的公开行为变化升级 `MAJOR`。

任何阶段都不得把 breaking change 隐藏在 `PATCH` 中。下列内容属于公开兼容
面，变化时必须评估版本升级：

- skill 名称、目录和职责边界；
- CLI 命令、必需参数、默认行为、stdout/stderr JSON 协议和退出码；
- 支持的 Python、操作系统与必需外部工具基线；
- artifact、state、journal、receipt、vault config 的 wire schema 与消费
  规则；
- 受管目录、持久文件位置、幂等身份和恢复行为；
- README、SKILL 和 SECURITY 中承诺的安全与隐私边界。

## 发行版本、契约版本与内容身份

这些标识解决的问题不同，不能互相替代：

| 标识 | 示例 | 含义 |
|---|---|---|
| 仓库发行版本 | `v0.1.0` | 一组可安装、可回滚的 skills、脚本、契约与文档 |
| Wire schema 版本 | `awesome-capture.artifact/v2` | 持久 JSON 对象的结构和语义协议 |
| `contract_digest` | 64 位 SHA-256 | 当前 wire schema 与语义验证器 bundle 的精确内容身份 |
| `runtime_digest` | 64 位 SHA-256 | standalone skill 内安全运行层的精确内容身份 |
| `implementation_digest` | 64 位 SHA-256 | smoke receipt 所验证实现范围的精确身份 |
| Commit SHA | Git object ID | 仓库某一次不可歧义的源码快照 |

`smoke/release-scope.json` 是候选 commit 中可审查的发布证据范围，不是新的
产品版本或 wire schema。它按 suite、平台或引擎选择必须提供当前 passing
receipt 的 case，并用 `base_version`、`base_commit` 绑定上一完整 release
边界。正式候选要求基线版本严格更低、恰为 changelog 中紧邻的上一版本，并由
对应不可移动轻量 tag 精确指向；仅自动发布流程建立前记录、未曾创建
tag/Release 的 `0.1.0` 历史版本边界使用代码中固定完整 SHA 的一次性
bootstrap。门禁会从该基线到候选 `HEAD` 推导执行面变化的最低组件集合，
scope 只能扩大该集合。Scope 文件本身参与 `implementation_digest`，因此
缩小或扩大范围都会使旧 receipt 失去“当前实现”资格，必须在最终 scope
固定后生成证据。

因此，`artifact/v2` 不表示仓库是 `v2.0.0`，仓库版本也不决定 artifact
版本。Schema 版本表达数据兼容边界；digest 用于逐字节复验；SemVer 面向使用
者表达整个发行包的兼容性。当前严格消费者要求自己的 `contract_digest` 与
producer 一致；任何 contract digest 变化都会使旧 producer 与新 consumer
无法直接互操作，即使结构变化表面上只是新增字段，也必须按 breaking contract
和仓库版本规则处理。

发行 tag 必须指向已经通过门禁的确切 commit SHA。Smoke receipt 必须匹配其
声明的 `implementation_digest` 和 outcome；时间戳只用于审计，不设置固定
过期天数。

## Changelog 规则

面向使用者的变化先写入 [`CHANGELOG.md`](CHANGELOG.md) 的
`[Unreleased]`。准备发布时，将这些条目移动到带日期的新版本小节，并重新
保留一个空的 `[Unreleased]`。Breaking change 必须同时说明迁移路径；不能
迁移时应明确写出拒绝策略和数据保留建议。

本项目尚未承诺长期支持分支。除非具体 release notes 另有说明，安全和兼容
修复以最新已发布版本为维护目标；需要精确复现时，应同时记录 release tag
与 commit SHA。
