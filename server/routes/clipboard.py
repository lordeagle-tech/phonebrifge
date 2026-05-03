"""
Clipboard sync route — read and write the laptop clipboard.
"""

import pyperclip
from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import require_auth

router = APIRouter()


@router.get("/clipboard")
async def get_clipboard(_token: str = Depends(require_auth)):
    try:
        text = pyperclip.paste()
        return {"text": text or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clipboard read failed: {e}")


@router.post("/clipboard")
async def set_clipboard(request: Request, _token: str = Depends(require_auth)):
    body = await request.json()
    text = body.get("text", "")
    try:
        pyperclip.copy(str(text))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clipboard write failed: {e}")
