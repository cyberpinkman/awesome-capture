# Knowledge-note schema

Use this structure as a default, adapting section names only when the material clearly requires it:

```markdown
# 标题

> [!summary]
> 用两到四句概括材料解决的问题、核心结论及适用边界。

## 核心结论

- 结论。证据：[00:01:20.000–00:01:46.000]

## 论证与证据

### 论点

- 论据及时间戳。
- 反例、条件或限制。

## 关键概念与方法

- **概念**：基于原材料的定义。证据：[时间戳]

## 可行动项

- 可执行动作，以及它来自事实还是推断。

## 待验证

- 原材料没有证明、需要外部核查的内容。

## 关联

- 可与哪些已有主题建立链接；没有可靠目标时留空，不虚构笔记名。
```

Rules:

- Cite important source claims with transcript timestamps.
- Mark model synthesis with phrases such as “推断” or “可进一步验证”.
- Put short verbatim excerpts in quotation marks; otherwise paraphrase.
- Do not copy the entire transcript into the knowledge note. The writer creates a separate preserved source note.
- The writer owns YAML frontmatter. Do not put frontmatter in the draft.
- Supply exactly one H1 and at least two H2 sections. On commit, the writer normalizes the H1 to the explicit `--title`; the source draft itself is not modified.
- Follow the vault build receipt's `link_style` when available. Without a receipt, use URL-encoded relative Markdown links for editor portability.
