#!/usr/bin/env python3
"""
Startup wrapper for Replit environment.
Runs the Phone Bridge server on PORT env variable (default 8080) with no TLS.
"""
import os
import sys
import random

from server import auth, webrtc_config

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

port = int(os.environ.get("PORT", 8080))
pin = os.environ.get("BRIDGE_PIN") or str(random.randint(100000, 999999))
session_timeout = int(os.environ.get("SESSION_TIMEOUT", 30))

auth.set_pin(pin)
auth.set_inactivity_timeout(session_timeout * 60)

print("\n" + "=" * 52)
print("  Phone Bridge")
print("=" * 52)
print(f"\n  Port       : {port}")
print(f"  Access PIN : {pin}")
print(f"  Mode       : HTTP (Replit proxy handles TLS)")
print(f"  Inactivity : auto-logout after {session_timeout} min idle")
print(f"  Audit log  : ~/.phone-bridge/audit.log")
print("\n  Open the preview URL to access the bridge.")
print("=" * 52 + "\n")

import uvicorn
from server.main import app

uvicorn.run(
    app,
    host="0.0.0.0",
    port=port,
    log_level="warning",
)
