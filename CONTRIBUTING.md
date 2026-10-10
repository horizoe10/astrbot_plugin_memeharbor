# Contributing

Keep chat interactions short and preserve existing data. Changes to collection, routing, ownership, recycling or migration should include regression coverage. Never commit real user images, databases, tokens, bot configuration or production logs.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_library.py -q
python -m ruff check .
python -m ruff format --check .
node --check pages/gallery/app.js
node tests/test_gallery.cjs
```

Integration tests in `tests/test_runtime.py` need an AstrBot Python environment. Set `ASTRBOT_SOURCE` to its checkout if it is not already importable. Tests isolate files in temporary directories, simulate events and stub AI calls. QQ transport conversion is tested with framework code; real Tencent delivery requires a separate manual smoke test.

For manual validation, enable the correct profile, collect an attached fixture image, search its tag, send its ID, page from two users, recycle/restore, and verify gallery edits appear in chat. Real group messages and paid AI calls remain explicit test actions. Retain the provenance in NOTICE.
