"""Normalize the legacy Camoufox cache layout for Camoufox 0.5+."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def main() -> None:
    try:
        from camoufox.multiversion import (
            BROWSERS_DIR,
            COMPAT_FLAG,
            CONFIG_FILE,
            INSTALL_DIR,
            REPO_CACHE_FILE,
            set_active,
            version_folder_name,
        )
    except ImportError:
        # Camoufox 0.4 uses the legacy flat layout.
        return

    legacy_metadata_path = INSTALL_DIR / "version.json"
    if not legacy_metadata_path.is_file():
        return

    metadata = json.loads(legacy_metadata_path.read_text(encoding="utf-8"))
    version = metadata["version"]
    build = metadata.get("build") or metadata.get("release") or metadata.get("tag")
    if not build:
        raise RuntimeError("Camoufox version metadata does not contain a build identifier")

    target = BROWSERS_DIR / "official" / version_folder_name(version, build)
    target.mkdir(parents=True, exist_ok=True)

    reserved = {
        BROWSERS_DIR.name,
        CONFIG_FILE.name,
        REPO_CACHE_FILE.name,
        COMPAT_FLAG.name,
    }
    for item in list(Path(INSTALL_DIR).iterdir()):
        if item.name in reserved:
            continue
        destination = target / item.name
        if destination.exists():
            raise RuntimeError(f"Camoufox migration target already exists: {destination}")
        shutil.move(str(item), str(destination))

    set_active(target.relative_to(INSTALL_DIR).as_posix())
    COMPAT_FLAG.touch()
    print(f"Normalized Camoufox installation: {target}")


if __name__ == "__main__":
    main()
