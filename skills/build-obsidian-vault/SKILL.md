---
name: build-obsidian-vault
description: Interview a user about knowledge-work goals and habits, then plan, create, or audit a maintainable local Obsidian vault made from ordinary Markdown, YAML properties, folders, and core-plugin-compatible templates. Use when a user asks to build, initialize, reorganize, or standardize an Obsidian knowledge base or vault. Prefer a new vault; extend an existing vault only after an explicit preview and never overwrite existing notes or settings.
---

# Build an Obsidian vault

Translate actual habits into a small, durable vault. Do not install community plugins or build an elaborate taxonomy before the user has demonstrated a need.

## Interview

Use short multi-turn questions. Ask one coherent group at a time and accept “use defaults” at any point.

1. **Purpose and outputs:** What should this vault help produce—learning, research, projects, content, decisions, or personal records? What are the two most common inputs and outputs?
2. **Capture and retrieval:** Where should quick captures land? Does the user retrieve by search, links/maps, folders, dates, or a mix? How much weekly maintenance is realistic?
3. **Conventions and constraints:** Preferred language, filename style, Wikilinks versus portable Markdown links, daily notes, attachment policy, sync/backup method, and target path.

Challenge contradictory requirements. For example, “zero maintenance” is incompatible with a deep manual taxonomy; choose a shallow inbox-plus-search workflow instead.

Read [profiles.md](references/profiles.md) after the first answer. Select the closest profile, then customize only what the user's answers justify.

## Build workflow

1. Write one config JSON conforming to [config-schema.md](references/config-schema.md). Keep it outside the target vault until validated.
2. Validate and preview:

   ```bash
   python3 scripts/vault_builder.py validate-config "<config.json>"
   python3 scripts/vault_builder.py plan "<config.json>" --vault "<absolute-vault-path>"
   ```

3. Summarize the proposed folders, capture destination, source/attachment locations, templates, link style, and path. Ask for confirmation because this creates local files.
4. For a new vault, apply:

   ```bash
   python3 scripts/vault_builder.py build "<config.json>" \
     --vault "<absolute-vault-path>" --apply
   ```

5. For an existing vault, run `audit` first:

   ```bash
   python3 scripts/vault_builder.py audit --vault "<absolute-vault-path>"
   ```

   Use `--extend-existing` only after showing all conflicts. The builder creates missing files but never overwrites differing content.
6. Run `audit` after the build and report the vault path, receipt, created/skipped files, conflicts, and the one manual action: open that folder as a vault in Obsidian. If daily notes are enabled in the profile, explain that the builder creates the folder and template but does not toggle or configure the Obsidian core plugin.

## Guardrails

- Keep source files portable: Markdown, ordinary attachments, and the skill-owned JSON receipt.
- Create the `.obsidian` marker directory, but do not write undocumented `.obsidian/*.json` application settings. Obsidian may change those private formats.
- Use Obsidian properties as flat YAML. Do not invent nested property schemas that the Properties UI does not support.
- Prefer stable folder paths and links. Avoid characters Obsidian documents as unsafe in links.
- Do not modify Obsidian's global vault registry or launch the GUI.
- Do not enable Sync, publish content, or copy settings from another vault.
- Reject filesystem roots, the user's home directory, `.obsidian` itself, and a target nested inside another vault.
- Do not overwrite. A conflict must be resolved by the user or by generating a new path.
- Keep templates minimal; add automation only after the base vault works.
