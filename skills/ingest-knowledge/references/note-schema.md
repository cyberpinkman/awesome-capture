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

## Evidence and source availability

- Treat the validated `awesome-capture.artifact/v2` transcript as the evidence boundary. The preserved source note is assembled from its text and timestamped segments, not by reopening the media.
- Ingest does not read `source.path`, the upstream video artifact, or companion subtitle/output files by default. The source media may be deleted before ingest.
- Use `--verify-source-media` only when the user explicitly requests a current media check. Missing media is recorded as unavailable; changed media is an integrity failure.
- Do not promote a title, description, external page, or model-generated context to transcript evidence. Put externally sourced or unsupported claims under “待验证”.

## Plan confirmation

The dry-run describes the complete intended write and returns `plan_sha256`.
Its internal `request_sha256` binds the canonical transcript artifact, exact
draft bytes, title, collection and source folders, normalized tags, link
style, and both destination paths. `plan_sha256` additionally binds the
observed destination files, matching receipt, build receipt, and pending
transactions so the exclusive-lock recheck can detect a stale vault view.

- Review the rendered destinations before approval.
- Pass the exact digest to the write with `--expected-plan-sha256`.
- If any bound input changes, run a new dry-run and obtain new confirmation.
- A digest is not a reusable approval token for another transcript, draft, title, layout, or vault state.

## Writer-owned identity

On commit, the writer creates both notes and owns their YAML identity fields, including the full 64-character `awesome_capture_id` and `source_sha256`.

- Do not add these fields to the draft and do not change or remove them after commit.
- Do not move or rename a managed note outside the writer workflow; the formal receipt records its relative path.
- The knowledge-note body may be edited after ingest. If both identity fields still match, reuse and audit preserve the edits and report `CONTENT_MODIFIED` rather than overwriting them.
- Missing or inconsistent identity frontmatter is a conflict, not an editable-content warning.
- The formal `awesome-capture.ingest-receipt/v1` stores the transcript, draft,
  request and confirmed-plan hashes, destinations, and initial file hashes. It
  is published last and is the only completion marker.
- The receipt filename is the complete stable ingest ID:

  `SHA256("awesome-capture.ingest-id/v1\0" + canonical_transcript_artifact_sha256)`

  Do not truncate, choose, or derive the ID from a media filename.
