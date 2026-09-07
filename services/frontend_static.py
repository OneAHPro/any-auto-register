"""Serve public build files before falling back to the client-side router."""
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse

_ASSET_SUFFIXES = {'.ttf', '.woff', '.woff2', '.svg', '.ico', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.css', '.js', '.map'}


def frontend_response(static_dir: str | Path, full_path: str) -> FileResponse:
    root = Path(static_dir).resolve()
    try:
        requested = (root / full_path).resolve()
        inside = requested.is_relative_to(root)
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(404, '文件不存在') from None
    if not inside:
        raise HTTPException(404, '文件不存在')
    if requested.is_file():
        return FileResponse(requested)
    if requested.suffix.lower() in _ASSET_SUFFIXES:
        raise HTTPException(404, '文件不存在')
    return FileResponse(root / 'index.html')
