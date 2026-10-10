# Moving to MemeHarbor 1.0

MemeHarbor uses `astrbot_plugin_memeharbor`, separately from `astrbot_plugin_image_replay`. Existing Chinese commands and `斗图` remain compatible; `/memeharbor` is the new unified entry point.

1. Back up the whole `data/plugin_data/astrbot_plugin_image_replay/` directory and its plugin configuration.
2. Disable Image Replay before installing/enabling MemeHarbor. Do not delete its data. Running both creates overlapping commands and libraries that can diverge.
3. Install MemeHarbor and enable it in the appropriate bot's profile. The new ID is not automatically added to profiles listing only the old ID.
4. If its dedicated data directory is empty, first startup copies legacy resources and JSON indexes. SQLite is copied with its backup API, including committed WAL data. The original directory remains intact.
5. Known legacy settings are copied only where a new value still equals its default; explicit new customizations are preserved. Review wake behavior, permissions and custom command mappings afterward.
6. Check search results and several IDs. Existing v2 catalog IDs, tags, descriptions, ownership, recycle state and send counts are retained. JSON-only libraries receive stable IDs during conversion.

A `migration_origin.json` file records the copy. Nonempty destinations are never automatically overwritten. Use web merge-import if MemeHarbor already has data, or restore a complete backup while both plugins are stopped.

## Formats and rollback

The catalog filename remains `library_v2.sqlite3`: this is a format identifier, not a release number. MemeHarbor starts its own release line at **1.0.0**.

ZIP import merges collections and may assign new IDs. It is not a rollback tool. Restore a complete matching data-directory backup for rollback; an older JSON-only plugin cannot read later SQLite edits.

```text
data/plugin_data/astrbot_plugin_memeharbor/
├── resource/                 Original images
├── library_v2.sqlite3        Catalog, tags and recycle state
├── migration_origin.json    Present after legacy import
├── image_tags.v1-backup.json Optional JSON migration backup
└── preview_cache/           Disposable previews/exports
```

迁移前请停用旧插件，迁移后请在对应会话配置里启用 MemeHarbor。如果 Alice 和 Fairy 使用不同配置，需要分别检查启用列表。原图库保留，不会因为改名主动删除。
