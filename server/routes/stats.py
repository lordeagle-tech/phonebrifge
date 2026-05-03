import psutil
from fastapi import APIRouter, Depends, Request

from ..deps import require_auth

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return None


@router.get("/stats/pair-info")
async def get_pair_info(request: Request, _token: str = Depends(require_auth)):
    """Return the phone's local-network IP as seen by the bridge.

    Used by the phone's Stats page to display a "Pair this phone" card so
    the user doesn't have to type the IP into the laptop's mirror-setup
    wizard by hand.
    """
    ip = _client_ip(request)
    looks_local = bool(ip) and (
        ip.startswith("192.168.")
        or ip.startswith("10.")
        or ip.startswith("169.254.")
        or any(ip.startswith(f"172.{n}.") for n in range(16, 32))
    )
    return {"ip": ip, "looks_local": looks_local}


@router.get("/stats")
async def get_stats(_token: str = Depends(require_auth)):
    cpu_percent = psutil.cpu_percent(interval=0.1)
    cpu_count = psutil.cpu_count()

    ram = psutil.virtual_memory()
    ram_info = {
        "total": ram.total,
        "used": ram.used,
        "free": ram.available,
        "percent": ram.percent,
    }

    disk = psutil.disk_usage("/")
    disk_info = {
        "total": disk.total,
        "used": disk.used,
        "free": disk.free,
        "percent": disk.percent,
    }

    net = psutil.net_io_counters()
    net_info = {
        "bytes_sent": net.bytes_sent,
        "bytes_recv": net.bytes_recv,
    }

    battery = None
    bat = psutil.sensors_battery()
    if bat:
        battery = {
            "percent": round(bat.percent, 1),
            "charging": bat.power_plugged,
            "time_left": int(bat.secsleft) if bat.secsleft and bat.secsleft > 0 else None,
        }

    return {
        "cpu": {"percent": cpu_percent, "count": cpu_count},
        "ram": ram_info,
        "disk": disk_info,
        "network": net_info,
        "battery": battery,
    }
