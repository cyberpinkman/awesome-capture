# Vault config schema

The builder's local generated Draft 2020-12 wire schema is
[`scripts/_contracts/schemas/vault-config-v1.schema.json`](../scripts/_contracts/schemas/vault-config-v1.schema.json).
The repository copy is the generation source, while a standalone skill reads
only this vendored copy. This document explains its cross-field semantics;
unknown keys and unknown versions are not accepted.

Required JSON shape:

```json
{
  "schema_version": "awesome-capture.vault-config/v1",
  "name": "我的知识库",
  "profile": "general",
  "language": "zh-CN",
  "folders": [
    "00 Inbox",
    "10 Projects",
    "20 Areas",
    "30 Resources",
    "40 Archive",
    "90 Sources",
    "99 Attachments",
    "_Templates"
  ],
  "inbox_folder": "00 Inbox",
  "sources_folder": "90 Sources",
  "attachments_folder": "99 Attachments",
  "templates_folder": "_Templates",
  "link_style": "wikilink",
  "daily_notes": {
    "enabled": false,
    "folder": "Daily",
    "format": "YYYY-MM-DD"
  }
}
```

Constraints:

- `profile`: `general`, `research`, `creator`, `projects`, or `custom`.
- All folders are safe relative paths with no `.` or `..` components.
- The four designated folders must appear in `folders`; an enabled daily-note folder must also appear.
- `link_style`: `wikilink` or `markdown`.
- The builder accepts extra unknown keys only by rejecting them. Extend the schema deliberately rather than silently ignoring typos.

The builder writes no secrets and does not configure cloud sync. It creates the
daily-note folder/template when enabled, but deliberately does not write
undocumented `.obsidian/*.json` settings or toggle the core plugin. The user may
select the generated folder/template in Obsidian after opening the vault.

Primary Obsidian references:

- Vaults are filesystem folders: <https://obsidian.md/help/Files%2Band%2Bfolders/Manage%2Bvaults>
- Properties are flat YAML: <https://obsidian.md/help/properties>
- Internal-link formats: <https://obsidian.md/help/links>
- Core Templates behavior: <https://obsidian.md/help/Plugins/Templates>
