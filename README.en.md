<p align="center"><img src="logo.png" width="150" alt="MemeHarbor logo"></p>
<h1 align="center">MemeHarbor</h1>
<p align="center"><strong>Collect once. Find fast. Reply fresh.</strong></p>
<p align="center">A QQ Official-first meme library for AstrBot · v1.0</p>
<p align="center"><a href="README.md">中文说明</a> · <a href="docs/MIGRATION.md">Migration guide</a> · <a href="https://github.com/horizoe10/astrbot_plugin_memeharbor/issues">Report an issue</a></p>

MemeHarbor turns a pile of reaction images into a searchable collection you can use during a conversation. Collect an image once, give it several tags, browse a numbered contact sheet, and send the original when the moment fits. Random replies prefer images your group has not seen recently.

## What you get

| Capability | How it helps in chat |
|---|---|
| **Capture without ceremony** | Collect an attached image, a quoted image, or a 60-second follow-up message. Identical files merge their tags. |
| **Search by intent** | Combine tags, exclude a mood, or match alternatives: `猫 憋笑`, `猫 -伤心`, `猫 \| 狗`. |
| **Browse before sending** | Numbered preview sheets, independent per-user paging, and stable IDs. GIF previews are static; originals stay animated. |
| **Keep replies fresh** | Per-group recent-history tracking, content-based batch deduplication, cooldowns, and a three-image sending cap. |
| **Organize with confidence** | Edit or merge tags, manage ownership, and use a recoverable recycle bin. Removing a tag never deletes a file. |
| **Manage outside chat** | An embedded gallery for search, batch edits, uploads, original downloads, and ZIP backup/import. |
| **Add AI when useful** | Optional descriptions, OCR, suggested tags, and natural-language retrieval through your AstrBot model. Off by default. |

## Install

Requires **AstrBot 4.29.0-beta.1 or later in the 4.x series** and **Python 3.12+**. Runtime dependencies are `aiohttp` and `Pillow`.

In AstrBot's plugin manager, install from this repository URL:

```text
https://github.com/horizoe10/astrbot_plugin_memeharbor
```

Alternatively, download **Code → Download ZIP** and use the local ZIP installer. Enable **MemeHarbor** in the configuration profile used by your bot or group. Installation does not automatically add it to every restricted plugin allowlist.

**Coming from Image Replay?** Disable the old plugin and back up its data first. MemeHarbor has a new plugin ID and copies an existing library into its own data directory on the first empty installation. Read the [migration guide](docs/MIGRATION.md).

## Your first collection

Mention the bot in a QQ Official group and attach an image with:

```text
/收集 猫 憋笑
```

Then try:

```text
/搜图 猫 憋笑
/下一页
/发图 12
/随机图 猫 3
```

Replace `12` with an actual ID from your preview. `/memeharbor` or `斗图帮助` shows help. Chinese chat commands are retained for existing users; an optional `/memeharbor` prefix also works, for example `/memeharbor 搜图 猫`.

### Trigger rules

- Explicit `/收集` works even when AstrBot's global wake-prefix list is empty. Full-width `／` is accepted.
- With `require_wake=false`, bare `收集 猫` and exact-tag quick replies work when AstrBot receives the message.
- With `require_wake=true`, bare commands and quick replies require a mention or framework wake condition.
- A message matching both command paths executes only once. Runtime command enablement, aliases, permissions, and plugin allowlists still apply.

## Command reference

| Command | Result |
|---|---|
| `收集 标签1 标签2` | Collect attached/quoted images, or wait 60 seconds for the same user's next image in that chat. |
| `取消收集` | Cancel waiting for an image. |
| `搜图 猫 憋笑` | Match both terms. Also accepts `找图`. |
| `搜图 猫 -伤心` | Include a term and exclude another. |
| `搜图 猫 \| 狗` | Match either group of conditions. |
| `搜图 #12` | Find a particular image ID. |
| `查看图片` | Browse the active library. |
| `下一页` / `上一页` / `图片翻页 2` | Navigate your own search for up to 10 minutes. |
| `发图 12` / `随机图 猫 3` | Send an original or up to three distinct matching images. |
| `图片详情 12` | Show tags, date, description, OCR and send count. |
| `标签列表 2` / `图库统计` | Browse tags or collection statistics. |
| `标签添加 12 可爱` / `标签移除 12 憋笑` | Edit one image's tags. |
| `标签重命名 旧 新` / `标签合并 旧 新` | Rename/merge a tag across the active library; administrator only. |
| `删除图片 12` / `恢复图片 12` | Recycle or restore an image. |
| `回收站` | Browse recycled images you can manage. |
| `识图 12` | Save an AI description and OCR; suggest tags without applying them. |
| `AI搜图 一只假装生气的猫` | Rank candidates using existing tags, descriptions and OCR. |

Search ignores case by default. Quick replies match exact tags, not casual OCR text. Tags are limited to 32 characters and 24 tags per image. The old `删除图片指令 标签 ALL` operation is retired: select IDs or remove tags explicitly instead.

## Built for QQ Official

MemeHarbor sends ordinary image components rather than OneBot merged-forward nodes. Each preview is one numbered contact sheet. Collection reads standard AstrBot Image/Reply components and QQ Official attachments.

QQ Official may omit the original attachment URL from a quote. If it does, send the image alongside the command or use two-message collection. The plugin does not call OneBot message-fetch APIs on QQ Official. Standard OneBot image/quote collection is retained; group-file attachments are not automatically treated as images.

Tencent's delivery, passive-reply quotas, rate limits and content review still apply. A successful adapter return is recorded as a send; failures swallowed by an adapter cannot be independently confirmed by the plugin.

## Web gallery

Open **Plugin details → Pages → MemeHarbor** in the authenticated AstrBot Dashboard.

- Browse 12 thumbnails per page, search and download originals.
- Batch-add/remove tags, recycle and restore images.
- Edit one image or request optional AI tag suggestions.
- Upload image files or merge a backup made by this plugin.
- Export images, tags, descriptions, OCR and recycle state together.

New uploads receive the tag `待整理`. Import merges by file content: IDs can change, while existing descriptions and recycle state remain intact. It is **not an in-place rollback**. Uploaded ZIPs are limited to 30 MB, 150 MB expanded, and 1,000 records. Back up the entire data directory for larger collections. Recycled images are not automatically purged.

## Settings that matter

| Setting | Default | Purpose |
|---|---|---|
| `require_wake` | `true` | Require a mention/wake for bare commands and quick replies. |
| `page_size` | `9` | Chat preview size, 3–12. |
| `max_send_count` | `3` | Per-request image cap, 1–3. |
| `recent_window` | `10` | Recent sends to avoid per group/chat. |
| `send_cooldown` | `2` | Seconds between one user's image sends in a chat. |
| `max_image_mb` | `15` | Per-image upload/download limit. |
| `download_use_proxy` | `true` | Follow AstrBot's proxy environment. |
| `allow_owner_manage` | `true` | Let collectors manage their own images. |
| `collect_admin_only` | `false` | Optionally restrict collection to administrators. |
| `ai_enabled` | `false` | Enable explicit AI requests. |
| `ai_provider_id` | empty | Existing AstrBot provider ID, not an API key. |
| `ai_admin_only` | `true` | Restrict chat AI commands to administrators. |

Group/user allowlists and blocklists remain available; QQ Official uses openids. The library is shared across groups. Recent-send state is separated by bot/group; paging and cooldowns also distinguish users. Restarting clears temporary paging, pending capture and recent-send state, but keeps images, tags and send counts.

When every candidate has appeared recently, random replies rotate through the least recently sent images. A one-image collection necessarily repeats. Explicit ID requests bypass random selection, but still obey cooldowns.

## Optional AI, explicit control

Choose a model already configured in AstrBot. Description/OCR requires a vision-capable model. Chat commands can use the current conversation's provider; web requests require an explicit provider ID.

AI is called only when requested. Image analysis sends the selected image to that provider and can incur its normal usage fees. Suggested tags need manual acceptance. AI search considers at most the newest 200 images' existing metadata and returns up to 24 results. It does not analyze all originals or maintain a vector database. Regular search and collection work without AI.

## Data and development

Data lives in `data/plugin_data/astrbot_plugin_memeharbor/`. The internal `library_v2.sqlite3` filename identifies the catalog format; the **MemeHarbor product version starts at 1.0.0**.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_library.py -q
python -m ruff check .
node tests/test_gallery.cjs
```

Runtime integration tests require an AstrBot environment. Set `ASTRBOT_SOURCE` to a framework checkout and run `tests/test_runtime.py` in its virtualenv. Automated tests use temporary data, simulated events, loopback HTTP and stubbed AI responses. They do not message real groups or call paid models. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits and provenance

Maintained by **[Ghostberry / horizoe10](https://github.com/horizoe10)**. MemeHarbor grew from the supplied **[Image Replay](https://github.com/cumany/astrbot_plugin_image_replay)** plugin by **cuman / cumany**, whose README also credits [Custom Menu](https://github.com/Futureppo/astrbot_plugin_custom_menu). The catalog, gallery, preview, routing fixes, migration and optional AI workflows are independently maintained here. This is not an official upstream release.

The supplied upstream package did not include a standalone license file. No blanket relicensing of upstream material is asserted by this repository. See [NOTICE](NOTICE).
