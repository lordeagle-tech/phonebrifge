"""
Configuration for WebRTC ICE servers, set by run.py at startup and read by
the /api/webrtc/config endpoint. Defaults to a public Google STUN server.
"""
from __future__ import annotations

from typing import List

DEFAULT_STUN = "stun:stun.l.google.com:19302"

_ice_servers: List[dict] = [{"urls": DEFAULT_STUN}]


def set_ice_servers(servers: List[dict]) -> None:
    global _ice_servers
    _ice_servers = list(servers)


def get_ice_servers() -> List[dict]:
    return list(_ice_servers)
