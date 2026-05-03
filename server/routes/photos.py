"""REST endpoints for the auto-import-photos feature."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import audit, photos
from ..deps import require_auth

router = APIRouter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "-"


@router.get("/photos/settings")
async def get_settings(_token: str = Depends(require_auth)):
    return photos.status()


@router.post("/photos/settings")
async def set_settings(
    request: Request,
    payload: dict = Body(...),
    _token: str = Depends(require_auth),
):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    enabled = bool(payload.get("enabled", False))
    destination = payload.get("destination") or photos.get_settings()["destination"]
    if not isinstance(destination, str):
        raise HTTPException(status_code=400, detail="destination must be a string")
    try:
        result = photos.set_settings(enabled=enabled, destination=destination)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log(
        "files",
        "photo_autoimport_toggle",
        ip=_ip(request),
        outcome="ok",
        detail={"enabled": result["enabled"], "destination": result["destination"]},
    )
    return photos.status()


@router.get("/photos/recent")
async def get_recent(_token: str = Depends(require_auth)):
    return {"recent": photos.get_recent()}
