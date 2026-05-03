import asyncio
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..deps import require_auth

router = APIRouter()

HOME = Path.home().resolve()
UPLOAD_CHUNK = 65536  # 64 KB chunks for streaming uploads


def _safe_path(path: Optional[str]) -> Path:
    """Resolve path and ensure it is strictly inside the user's home directory."""
    if not path:
        return HOME
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(HOME)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path outside home directory")
    return resolved


def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


class _Cancelled(Exception):
    pass


def _add_path_to_zip(zf: zipfile.ZipFile, src: Path, arc_root: str, cancel: threading.Event) -> None:
    """Add a file or directory tree to the zip, rooted at arc_root inside the archive."""
    if cancel.is_set():
        raise _Cancelled()
    if src.is_file():
        try:
            zf.write(str(src), arcname=arc_root)
        except (PermissionError, OSError):
            pass
        return
    if src.is_dir():
        # Include the (possibly empty) directory entry
        try:
            zf.writestr(arc_root.rstrip("/") + "/", "")
        except (PermissionError, OSError):
            pass
        for root, dirs, files in os.walk(str(src)):
            if cancel.is_set():
                raise _Cancelled()
            root_path = Path(root)
            rel = root_path.relative_to(src)
            for d in dirs:
                arc = f"{arc_root}/{(rel / d).as_posix()}/"
                try:
                    zf.writestr(arc, "")
                except (PermissionError, OSError):
                    pass
            for fname in files:
                if cancel.is_set():
                    raise _Cancelled()
                fp = root_path / fname
                arc = f"{arc_root}/{(rel / fname).as_posix()}"
                try:
                    zf.write(str(fp), arcname=arc)
                except (PermissionError, OSError):
                    pass


def _build_zip_sync(items: List[Path], tmp_path: Path, cancel: threading.Event) -> None:
    """Build the zip synchronously into tmp_path. Raises _Cancelled if aborted."""
    with zipfile.ZipFile(str(tmp_path), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # De-duplicate arcnames if multiple selected items share a name
        seen = {}
        for item in items:
            if cancel.is_set():
                raise _Cancelled()
            base = item.name or "root"
            count = seen.get(base, 0)
            arc_root = base if count == 0 else f"{base} ({count})"
            seen[base] = count + 1
            _add_path_to_zip(zf, item, arc_root, cancel)


async def _build_zip_cancellable(items: List[Path], request: Request) -> Path:
    """Build a zip in a background thread; cancel & cleanup if the client disconnects."""
    tmp = tempfile.NamedTemporaryFile(prefix="phone-bridge-", suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)

    cancel = threading.Event()
    error: List[BaseException] = []
    done = threading.Event()

    def worker():
        try:
            _build_zip_sync(items, tmp_path, cancel)
        except _Cancelled:
            pass
        except BaseException as e:  # noqa: BLE001
            error.append(e)
        finally:
            done.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    try:
        while not done.is_set():
            if await request.is_disconnected():
                cancel.set()
                # Wait for thread to notice cancel and stop, then cleanup
                await asyncio.get_running_loop().run_in_executor(None, done.wait, 5.0)
                _cleanup(tmp_path)
                raise HTTPException(status_code=499, detail="Client cancelled")
            await asyncio.sleep(0.2)
    except HTTPException:
        raise
    except BaseException:
        cancel.set()
        await asyncio.get_running_loop().run_in_executor(None, done.wait, 5.0)
        _cleanup(tmp_path)
        raise

    if error:
        _cleanup(tmp_path)
        raise error[0]
    if cancel.is_set():
        _cleanup(tmp_path)
        raise HTTPException(status_code=499, detail="Client cancelled")
    return tmp_path


def _cleanup(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


@router.get("/files")
async def list_files(path: Optional[str] = None, _token: str = Depends(require_auth)):
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    entries = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size if not entry.is_dir() else None,
                    "size_fmt": _fmt_size(stat.st_size) if not entry.is_dir() else None,
                    "modified": int(stat.st_mtime),
                })
            except (PermissionError, OSError):
                pass
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    parent = str(target.parent) if target != HOME else None

    return {
        "path": str(target),
        "parent": parent,
        "home": str(HOME),
        "entries": entries,
    }


@router.get("/files/download")
async def download_file(path: str = Query(...), _token: str = Depends(require_auth)):
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(target), filename=target.name, media_type="application/octet-stream")


@router.get("/files/download-folder")
async def download_folder(request: Request, path: str = Query(...), _token: str = Depends(require_auth)):
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a folder")

    zip_path = await _build_zip_cancellable([target], request)
    filename = f"{target.name or 'home'}.zip"
    return FileResponse(
        path=str(zip_path),
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(_cleanup, zip_path),
    )


@router.post("/files/download-zip")
async def download_zip(
    request: Request,
    payload: dict = Body(...),
    _token: str = Depends(require_auth),
):
    raw_paths = payload.get("paths") or []
    if not isinstance(raw_paths, list) or not raw_paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    items: List[Path] = []
    for p in raw_paths:
        if not isinstance(p, str):
            continue
        target = _safe_path(p)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {p}")
        items.append(target)

    if not items:
        raise HTTPException(status_code=400, detail="No valid paths provided")

    zip_path = await _build_zip_cancellable(items, request)

    if len(items) == 1:
        base = items[0].name or "download"
        filename = f"{base}.zip"
    else:
        filename = "files.zip"

    return FileResponse(
        path=str(zip_path),
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(_cleanup, zip_path),
    )


@router.post("/files/upload")
async def upload_file(
    path: str = Query(...),
    file: UploadFile = File(...),
    _token: str = Depends(require_auth),
):
    target_dir = _safe_path(path)
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Target path is not a directory")

    dest = target_dir / file.filename
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(UPLOAD_CHUNK)
            if not chunk:
                break
            out.write(chunk)

    return {"ok": True, "path": str(dest), "name": file.filename}


@router.delete("/files")
async def delete_file(path: str = Query(...), _token: str = Depends(require_auth)):
    target = _safe_path(path)

    if target.resolve() == HOME:
        raise HTTPException(status_code=403, detail="Cannot delete the home directory itself")

    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    if target.is_dir():
        shutil.rmtree(str(target))
    else:
        os.remove(str(target))

    return {"ok": True}
