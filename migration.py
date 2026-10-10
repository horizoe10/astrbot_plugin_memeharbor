"""Copy an existing Image Replay library into a fresh MemeHarbor installation."""

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

LEGACY_NAME = "astrbot_plugin_image_replay"


def import_legacy_library(destination: Path) -> bool:
    """Import once into an empty destination, retaining the source library.

    Args:
        destination: The new plugin's dedicated, initially empty data directory.

    Returns:
        Whether a legacy library was copied. SQLite's backup API includes WAL data.

    Raises:
        ValueError: The old resource directory contains symbolic links.
        OSError: Files could not be copied or the destination changed during import.
    """
    legacy = destination.parent / LEGACY_NAME
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()) or not legacy.is_dir():
        return False
    if (
        not (legacy / "resource").is_dir()
        and not (legacy / "library_v2.sqlite3").is_file()
    ):
        return False
    resources = legacy / "resource"
    if resources.is_symlink() or (
        resources.exists() and any(path.is_symlink() for path in resources.rglob("*"))
    ):
        raise ValueError(
            "Legacy resource directory contains symbolic links; migrate it manually"
        )
    with tempfile.TemporaryDirectory(
        prefix=".memeharbor-migrate-", dir=destination.parent
    ) as directory:
        stage = Path(directory)
        if resources.is_dir():
            shutil.copytree(resources, stage / "resource")
        for name in ("image_tags.json", "image_tags.v1-backup.json"):
            path = legacy / name
            if path.is_file():
                shutil.copy2(path, stage / name)
        database = legacy / "library_v2.sqlite3"
        if database.is_file():
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as source:
                with closing(sqlite3.connect(stage / database.name)) as target:
                    source.backup(target)
        (stage / "migration_origin.json").write_text(
            json.dumps({"source": LEGACY_NAME, "method": "copy", "version": 1}),
            encoding="utf-8",
        )
        # Replace only the empty directory created above; never overwrite a library.
        destination.rmdir()
        stage.replace(destination)
    return True
