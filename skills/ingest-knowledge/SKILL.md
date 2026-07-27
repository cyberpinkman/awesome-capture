---
name: ingest-knowledge
description: Structure a completed, verified transcript artifact into evidence-linked knowledge notes and write them idempotently into an existing local Obsidian vault. Use when a user asks to archive, deposit, save, or turn transcribed content into durable Obsidian knowledge; when $transcribe-media hands off a completed transcript after user confirmation; or when validating and deduplicating an intended transcript-based knowledge note. Never write before the user has selected or confirmed the target vault.
---

# Ingest knowledge

Create one durable knowledge note plus one preserved source note. Treat the source artifact as evidence and keep model inference visibly separate.

## Workflow

1. Resolve an existing vault. Prefer a path the user already supplied or a configured `OBSIDIAN_VAULT_PATH`. If neither exists, ask for the vault path. Do not scan the entire home directory.
2. Validate a transcript artifact before using it:

   ```bash
   python3 scripts/knowledge_writer.py validate-transcript "<transcript.json>"
   ```

3. Read the full transcript. Draft a Markdown body outside the vault using [note-schema.md](references/note-schema.md).
   - Use only claims supported by transcript timestamps.
   - Label inference as inference.
   - Put unresolved or externally sourced claims under “待验证”.
   - Do not invent quotations, speakers, dates, or context.
   - Include exactly one H1 and at least two H2 sections. The writer replaces the draft H1 with the explicit `--title` so the body, frontmatter, and filename remain consistent.
4. Choose a collection folder inside the vault. Use the vault's existing convention; otherwise use `00 Inbox`. Keep a separate source folder, default `90 Sources`. Link style defaults to the `$build-obsidian-vault` receipt when present and to portable Markdown links otherwise; pass `--link-style` only to override it.
5. Run a dry run:

   ```bash
   python3 scripts/knowledge_writer.py commit \
     --transcript "<transcript.json>" --document "<draft.md>" \
     --vault "<vault>" --title "<title>" --collection "<folder>" --dry-run
   ```

6. If validation and destinations are correct, rerun without `--dry-run`. This write is already authorized when the user explicitly invoked this skill or answered yes to `$transcribe-media`.
7. Report the absolute knowledge-note path, preserved-source path, receipt path, and whether the operation created or reused an idempotent result.

## Reliability rules

- Require a completed source artifact. Never turn a failed or metadata-only download into a knowledge note.
- Generate stable identity from source hash plus schema version. Repeating the same ingest returns the prior receipt instead of creating duplicates.
- Never overwrite an unrelated note. A path collision without a matching receipt is a hard error.
- Keep writes within the resolved vault and reject absolute or parent-traversing collection paths.
- Reject filesystem roots, the user's home directory, and `.obsidian` itself as write targets.
- Write through same-filesystem staging and no-clobber atomic links. Roll back only files whose hashes still match the transaction. Never edit Obsidian's global registry or launch its GUI.
- Keep all Cookie values, API keys, private headers, and hidden diagnostics out of notes.
- Do not install community plugins. The output must remain ordinary Markdown/YAML.
