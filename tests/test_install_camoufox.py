from __future__ import annotations

import os
from urllib.error import HTTPError
import zipfile

from scripts import install_camoufox


def test_optional_addon_download_failure_keeps_browser_install_usable(
    monkeypatch,
    tmp_path,
):
    install_dir = tmp_path / "camoufox"
    downloads: list[str] = []

    def fake_download(url, destination):
        downloads.append(url)
        if "addons.mozilla.org" in url:
            raise HTTPError(url, 451, "Unavailable", {}, None)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("camoufox-bin", "browser")
            archive.writestr("application.ini", "[App]")

    monkeypatch.setenv("CAMOUFOX_VERSION", "135.0.1")
    monkeypatch.setenv("CAMOUFOX_RELEASE", "beta.24")
    monkeypatch.setattr(
        install_camoufox,
        "user_cache_dir",
        lambda _name: str(install_dir),
    )
    monkeypatch.setattr(
        install_camoufox.urllib.request,
        "urlretrieve",
        fake_download,
    )

    install_camoufox.main()

    binary = install_dir / "camoufox-bin"
    assert binary.read_text(encoding="utf-8") == "browser"
    assert os.access(binary, os.X_OK)
    assert len(downloads) == 2

