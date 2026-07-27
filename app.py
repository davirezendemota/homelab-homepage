#!/usr/bin/env python3
"""Homelab homepage: containers Docker ativos + pesquisa."""

from __future__ import annotations

import html
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import sys
import time
import urllib.parse
from collections.abc import Iterator

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
HOST_ROOT = os.environ.get("HOST_ROOT", "/host").rstrip("/")
SAFE_CONTAINER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LOG_TAIL = 200


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker_get(path: str):
    conn = UnixHTTPConnection(DOCKER_SOCKET)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Docker API {resp.status}: {body.decode(errors='replace')}")
        return json.loads(body)
    finally:
        conn.close()


def docker_open(path: str, timeout: float | None = 5.0):
    conn = UnixHTTPConnection(DOCKER_SOCKET, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    if resp.status >= 400:
        body = resp.read().decode(errors="replace")
        conn.close()
        raise RuntimeError(f"Docker API {resp.status}: {body}")
    return conn, resp


def published_ports(container: dict) -> list[int]:
    ports: list[int] = []
    seen: set[int] = set()
    for p in container.get("Ports") or []:
        public = p.get("PublicPort")
        if not public or (p.get("Type") or "tcp").lower() != "tcp":
            continue
        if public in seen:
            continue
        seen.add(public)
        ports.append(public)
    return sorted(ports)


def request_host(handler: http.server.BaseHTTPRequestHandler) -> str:
    raw = handler.headers.get("Host", "localhost")
    host = raw.split(":")[0].strip() or "localhost"
    return "localhost" if host in {"0.0.0.0", "[::]", "::"} else host


def parse_health(status: str) -> str | None:
    s = status.lower()
    for h in ("healthy", "unhealthy", "starting"):
        if f"({h})" in s:
            return h
    return None


def running_containers() -> list[dict]:
    rows = []
    for c in docker_get("/containers/json"):
        labels = c.get("Labels") or {}
        names = [n.lstrip("/") for n in (c.get("Names") or [])]
        cid = c.get("Id") or ""
        name = names[0] if names else cid[:12]
        stack = labels.get("com.docker.compose.project") or "sem stack"
        status = c.get("Status") or ""
        rows.append(
            {
                "id": cid[:12],
                "name": name,
                "image": c.get("Image") or "",
                "status": status,
                "ports": published_ports(c),
                "stack": stack,
                "health": parse_health(status),
            }
        )
    return sorted(rows, key=lambda r: (r["stack"].lower(), r["name"].lower()))


def container_has_tty(ref: str) -> bool:
    info = docker_get(f"/containers/{urllib.parse.quote(ref, safe='')}/json")
    return bool((info.get("Config") or {}).get("Tty"))


def iter_docker_log_chunks(resp, tty: bool) -> Iterator[bytes]:
    if tty:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            yield chunk
        return

    while True:
        header = resp.read(8)
        if len(header) < 8:
            break
        size = int.from_bytes(header[4:8], "big")
        if size <= 0:
            continue
        payload = resp.read(size)
        if not payload:
            break
        yield payload


def open_container_logs(ref: str, tail: int = LOG_TAIL):
    if not SAFE_CONTAINER_REF.match(ref):
        raise ValueError("Referência de container inválida")
    quoted = urllib.parse.quote(ref, safe="")
    tty = container_has_tty(ref)
    qs = urllib.parse.urlencode(
        {
            "stdout": "1",
            "stderr": "1",
            "follow": "1",
            "tail": str(tail),
            "timestamps": "0",
        }
    )
    conn, resp = docker_open(f"/containers/{quoted}/logs?{qs}", timeout=None)
    return conn, resp, tty


def _read_cpu_times() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return total, idle


def cpu_percent() -> tuple[int, int]:
    ncpu = os.cpu_count() or 1
    try:
        t0, i0 = _read_cpu_times()
        time.sleep(0.12)
        t1, i1 = _read_cpu_times()
        dt, di = t1 - t0, i1 - i0
        pct = 0 if dt <= 0 else round(max(0.0, min(100.0, (1 - di / dt) * 100)))
        return pct, ncpu
    except OSError:
        return 0, ncpu


def ram_stats() -> tuple[int, float, float]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    info[key] = int(rest.strip().split()[0])
    except OSError:
        return 0, 0.0, 0.0
    total_kb = info.get("MemTotal", 0)
    avail_kb = info.get("MemAvailable", 0)
    if total_kb <= 0:
        return 0, 0.0, 0.0
    used_kb = max(0, total_kb - avail_kb)
    pct = round(used_kb / total_kb * 100)
    return pct, used_kb / (1024**2), total_kb / (1024**2)


MAIN_DISK_COLOR = "#3fb950"
MOUNT_COLORS = ("#6cb6ff", "#a371f7", "#e3b341", "#f778ba", "#56d4dd", "#ffa657")

PSEUDO_FSTYPES = frozenset(
    {
        "proc",
        "sysfs",
        "devtmpfs",
        "devpts",
        "tmpfs",
        "cgroup",
        "cgroup2",
        "pstore",
        "bpf",
        "tracefs",
        "debugfs",
        "securityfs",
        "mqueue",
        "hugetlbfs",
        "configfs",
        "fusectl",
        "binfmt_misc",
        "autofs",
        "rpc_pipefs",
        "squashfs",
        "nsfs",
        "efivarfs",
        "overlay",
    }
)


def _mount_usage(mountpoint: str) -> tuple[int, float, float] | None:
    try:
        usage = shutil.disk_usage(mountpoint)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    pct = round(usage.used / usage.total * 100)
    return pct, usage.used / (1024**3), usage.total / (1024**3)


def _skip_mount(mountpoint: str, fstype: str, using_host: bool) -> bool:
    if fstype in PSEUDO_FSTYPES:
        return True
    if using_host:
        return False
    if mountpoint.startswith("/app"):
        return True
    if mountpoint.endswith((".py", ".js", ".json", ".yaml", ".yml")):
        return True
    return False


def disk_stats() -> tuple[int, float, float]:
    mount_path = HOST_ROOT if _using_host_storage() else "/"
    stats = _mount_usage(mount_path)
    if stats is None:
        return 0, 0.0, 0.0
    return stats


def _using_host_storage() -> bool:
    return bool(HOST_ROOT) and os.path.isdir(HOST_ROOT)


HOST_MOUNT_PARENTS = ("mnt", "media", "srv")
SKIP_STORAGE_MOUNTS = frozenset({"/boot", "/boot/efi"})


def _fstab_mountpoints(fstab_path: str) -> list[str]:
    points: list[str] = []
    try:
        with open(fstab_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                mountpoint = parts[1]
                if mountpoint not in points:
                    points.append(mountpoint)
    except OSError:
        pass
    return points


def _host_mount_path(host_root: str, mountpoint: str) -> str:
    if mountpoint == "/":
        return host_root
    return f"{host_root}{mountpoint}"


def _discover_mountpoints(host_root: str) -> list[str]:
    mounts: list[str] = []

    def add(path: str) -> None:
        if path not in mounts and os.path.isdir(path) and os.path.ismount(path):
            mounts.append(path)

    add(host_root)

    for mountpoint in _fstab_mountpoints(f"{host_root}/etc/fstab"):
        add(_host_mount_path(host_root, mountpoint))

    for parent_name in HOST_MOUNT_PARENTS:
        parent = os.path.join(host_root, parent_name)
        if not os.path.isdir(parent):
            continue
        add(parent)
        try:
            for name in os.listdir(parent):
                add(os.path.join(parent, name))
        except OSError:
            continue

    return mounts


def _display_path(mount_path: str, host_root: str) -> str:
    if mount_path == host_root:
        return "/"
    if mount_path.startswith(f"{host_root}/"):
        return mount_path[len(host_root) :]
    return mount_path


def _should_skip_storage_mount(path: str) -> bool:
    return path in SKIP_STORAGE_MOUNTS


def _storage_mounts_from_host(host_root: str) -> tuple[dict, list[dict]]:
    rows: list[dict] = []
    seen_devices: set[int] = set()

    for mount_path in _discover_mountpoints(host_root):
        try:
            device_id = os.stat(mount_path).st_dev
        except OSError:
            continue
        if device_id in seen_devices:
            continue
        stats = _mount_usage(mount_path)
        if stats is None:
            continue
        seen_devices.add(device_id)
        pct, used_gb, total_gb = stats
        display_path = _display_path(mount_path, host_root)
        if _should_skip_storage_mount(display_path):
            continue
        rows.append(
            {
                "path": display_path,
                "pct": pct,
                "used_gb": used_gb,
                "total_gb": total_gb,
            }
        )

    return _split_storage_rows(rows)


def _storage_mounts_from_proc() -> tuple[dict, list[dict]]:
    rows: list[dict] = []
    seen_devices: set[str] = set()

    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                if device in {"none", "udev"}:
                    continue
                if _skip_mount(mountpoint, fstype, using_host=False):
                    continue
                if _should_skip_storage_mount(mountpoint):
                    continue
                if device in seen_devices:
                    continue
                stats = _mount_usage(mountpoint)
                if stats is None:
                    continue
                seen_devices.add(device)
                pct, used_gb, total_gb = stats
                rows.append(
                    {
                        "path": mountpoint,
                        "pct": pct,
                        "used_gb": used_gb,
                        "total_gb": total_gb,
                    }
                )
    except OSError:
        rows = []

    return _split_storage_rows(rows)


def _split_storage_rows(rows: list[dict]) -> tuple[dict, list[dict]]:
    rows.sort(key=lambda row: (row["path"] != "/", row["path"]))

    main: dict | None = None
    others: list[dict] = []
    color_idx = 0
    for row in rows:
        if row["path"] == "/":
            main = {
                **row,
                "label": "/",
                "color": MAIN_DISK_COLOR,
            }
            continue
        others.append(
            {
                **row,
                "label": row["path"],
                "color": MOUNT_COLORS[color_idx % len(MOUNT_COLORS)],
            }
        )
        color_idx += 1

    if main is None:
        pct, used_gb, total_gb = disk_stats()
        main = {
            "path": "/",
            "label": "/",
            "pct": pct,
            "used_gb": used_gb,
            "total_gb": total_gb,
            "color": MAIN_DISK_COLOR,
        }

    return main, others


def storage_mounts() -> tuple[dict, list[dict]]:
    if _using_host_storage():
        return _storage_mounts_from_host(HOST_ROOT)
    return _storage_mounts_from_proc()


def meter_color(pct: int) -> str:
    if pct >= 85:
        return "#f85149"
    if pct >= 65:
        return "#e3b341"
    return "#3fb950"


def _storage_aggregate(main: dict, extras: list[dict]) -> tuple[int, float, float]:
    mounts = [main, *extras]
    total_used = sum(m["used_gb"] for m in mounts)
    total_gb = sum(m["total_gb"] for m in mounts)
    pct = round(total_used / total_gb * 100) if total_gb > 0 else 0
    return pct, total_used, total_gb


def build_meters(container_count: int) -> list[dict]:
    cpu, ncpu = cpu_percent()
    ram_pct, ram_used, ram_total = ram_stats()
    main_disk, extra_mounts = storage_mounts()
    storage_pct, storage_used, storage_total = _storage_aggregate(main_disk, extra_mounts)
    return [
        {
            "label": "CPU",
            "display": f"{cpu}%",
            "sub": f"{ncpu} cores",
            "showSub": True,
            "color": meter_color(cpu),
            "barWidth": f"{cpu}%",
            "showBar": True,
            "showChart": True,
            "chartKey": "cpu",
            "pct": cpu,
            "showCaption": False,
            "caption": "",
        },
        {
            "label": "RAM",
            "display": f"{ram_pct}%",
            "sub": f"{ram_used:.1f} / {ram_total:.0f} GB",
            "showSub": True,
            "color": meter_color(ram_pct),
            "barWidth": f"{ram_pct}%",
            "showBar": True,
            "showChart": True,
            "chartKey": "ram",
            "pct": ram_pct,
            "showCaption": False,
            "caption": "",
        },
        {
            "label": "Armazenamento",
            "type": "storage",
            "display": f"{storage_pct}%",
            "sub": f"{storage_used:.1f} / {storage_total:.0f} GB",
            "showSub": True,
            "color": meter_color(storage_pct),
            "barWidth": f"{storage_pct}%",
            "showBar": False,
            "showCaption": False,
            "caption": "",
            "main": main_disk,
            "mounts": extra_mounts,
        },
        {
            "label": "Containers",
            "display": str(container_count),
            "sub": "",
            "showSub": False,
            "color": "#e6e9ef",
            "barWidth": "0%",
            "showBar": False,
            "showCaption": True,
            "caption": f"{container_count} em execução",
        },
    ]


def app_build() -> str:
    try:
        return str(int(os.stat(__file__).st_mtime))
    except OSError:
        return "0"


_MODULE_BUILD = app_build()


def reload_if_stale() -> None:
    if app_build() != _MODULE_BUILD:
        os.execv(sys.executable, [sys.executable, "-u", __file__])


def page_payload(req_host: str) -> dict:
    error: str | None = None
    containers: list[dict] = []
    try:
        containers = running_containers()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    return {
        "host": req_host,
        "build": app_build(),
        "loaded_build": _MODULE_BUILD,
        "error": error,
        "containers": containers,
        "meters": build_meters(len(containers)),
    }


def render_page(req_host: str) -> str:
    data_json = json.dumps(page_payload(req_host), ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Homelab · Containers</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; }}
    html {{
      min-height: 100%;
      background: #0c0f14;
      background-image: radial-gradient(circle at 12% -5%, rgba(56,86,140,.18), transparent 42%);
    }}
    body {{
      margin: 0;
      min-height: 100%;
      font-family: 'Inter', system-ui, sans-serif;
      color: #e6e9ef;
    }}
    ::placeholder {{ color: #5b6472; }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: .35; }}
    }}
    .page {{
      min-height: 100vh;
      padding: 40px 48px 64px;
    }}
    .wrap {{ max-width: 1320px; margin: 0 auto; }}
    .header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 24px;
      flex-wrap: wrap;
      margin-bottom: 28px;
    }}
    .eyebrow {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 6px;
    }}
    .eyebrow-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #3fb950;
      box-shadow: 0 0 0 4px rgba(63,185,80,.15);
    }}
    .eyebrow-text {{
      font-size: 12px;
      font-weight: 600;
      letter-spacing: .12em;
      text-transform: uppercase;
      color: #7d8695;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -.02em;
    }}
    .clock {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      color: #7d8695;
    }}
    .header-settings-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 30px;
      margin-left: 2px;
      padding: 0;
      color: #7d8695;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 8px;
      cursor: pointer;
      transition: color .12s, background .12s, border-color .12s;
    }}
    .header-settings-btn:hover {{
      color: #c9d1de;
      background: #1a2230;
      border-color: #2a3544;
    }}
    .header-settings-btn svg {{
      width: 16px;
      height: 16px;
      display: block;
    }}
    .clock-dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #3fb950;
      animation: pulse 2s ease-in-out infinite;
    }}
    .meters {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 14px;
      margin-bottom: 26px;
    }}
    .meter {{
      background: #141922;
      border: 1px solid #1e2530;
      border-radius: 12px;
      padding: 16px 18px;
      min-width: 0;
      overflow: hidden;
    }}
    .meter-top {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
    }}
    .meter-label {{
      font-size: 12px;
      color: #7d8695;
      font-weight: 600;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    .meter-sub {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: #5b6472;
    }}
    .meter-value {{
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -.02em;
    }}
    .meter-value-row {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 6px;
      min-width: 0;
    }}
    .meter-value-row .meter-value {{
      flex-shrink: 0;
    }}
    .meter-bar {{
      flex: 1;
      min-width: 0;
      height: 6px;
      border-radius: 20px;
      background: #1e2530;
      overflow: hidden;
    }}
    .meter-bar > span {{
      display: block;
      height: 100%;
      border-radius: 20px;
    }}
    .meter:has(.meter-chart-wrap) {{
      padding: 14px 12px 10px;
    }}
    .meter-chart-wrap {{
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr) auto;
      column-gap: 4px;
      row-gap: 2px;
      margin-top: 8px;
      height: 72px;
      min-width: 0;
      overflow: hidden;
    }}
    .meter-chart-axis {{
      grid-column: 1;
      grid-row: 1;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      font-family: 'JetBrains Mono', monospace;
      font-size: 9px;
      line-height: 1;
      color: #5b6472;
      pointer-events: none;
    }}
    .meter-chart {{
      grid-column: 2;
      grid-row: 1;
      display: block;
      width: 100%;
      height: 100%;
      min-width: 0;
      overflow: hidden;
    }}
    .meter-chart-times {{
      grid-column: 2;
      grid-row: 2;
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 9px;
      line-height: 1;
      color: #5b6472;
      pointer-events: none;
    }}
    .meter-caption {{
      font-size: 12px;
      color: #7d8695;
      font-weight: 500;
      margin-top: 12px;
    }}
    .storage-bars {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: 12px;
    }}
    .storage-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      border-radius: 6px;
      margin: 0 -6px;
      padding: 2px 6px;
      transition: background .12s ease;
    }}
    .storage-row:hover {{
      background: rgba(255, 255, 255, .04);
    }}
    .storage-label {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: #8b94a3;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      width: 92px;
      flex-shrink: 0;
    }}
    .storage-bar {{
      flex: 1;
      min-width: 0;
      height: 6px;
      border-radius: 20px;
      background: #1e2530;
      overflow: hidden;
    }}
    .storage-bar > span {{
      display: block;
      height: 100%;
      border-radius: 20px;
    }}
    .storage-pct {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 600;
      color: #c9d1de;
      min-width: 38px;
      text-align: right;
      flex-shrink: 0;
    }}
    .storage-tooltip {{
      position: fixed;
      z-index: 40;
      padding: 8px 12px;
      border-radius: 8px;
      background: #1a2030;
      border: 1px solid #2a3544;
      box-shadow: 0 8px 24px rgba(0, 0, 0, .35);
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      line-height: 1.4;
      color: #c9d1de;
      white-space: nowrap;
      pointer-events: none;
      transform: translate(-50%, calc(-100% - 10px));
    }}
    .storage-tooltip[hidden] {{
      display: none;
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
      margin-bottom: 44px;
      justify-content: space-between;
    }}
    .search-wrap {{
      position: relative;
      flex: 1;
      min-width: 260px;
      max-width: 420px;
    }}
    .search-icon {{
      position: absolute;
      left: 14px;
      top: 50%;
      transform: translateY(-50%);
      color: #5b6472;
      font-size: 15px;
      pointer-events: none;
    }}
    .search-input {{
      width: 100%;
      background: #141922;
      border: 1px solid #1e2530;
      border-radius: 10px;
      padding: 11px 14px 11px 36px;
      color: #e6e9ef;
      font-size: 14px;
      font-family: 'Inter', sans-serif;
      outline: none;
    }}
    .search-input:focus {{
      border-color: #2a3544;
    }}
    .sort {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .sort-label {{
      font-size: 12px;
      font-weight: 600;
      letter-spacing: .04em;
      text-transform: uppercase;
      color: #5b6472;
    }}
    .sort-btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: 'Inter', sans-serif;
      font-size: 12.5px;
      font-weight: 600;
      color: #c9d1de;
      background: #141922;
      border: 1px solid #1e2530;
      border-radius: 20px;
      padding: 5px 12px;
      cursor: pointer;
      white-space: nowrap;
      transition: all .12s;
    }}
    .sort-btn.active {{
      color: #0c0f14;
      background: #6cb6ff;
      border-color: #6cb6ff;
    }}
    .sort-btn .arrow {{
      font-size: 10px;
      opacity: .85;
    }}
    .stack-block {{ margin-bottom: 44px; }}
    .stack-head {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
      padding: 0 4px;
    }}
    .stack-hide-btn,
    .stack-collapse-btn {{
      width: 28px;
      height: 28px;
      flex-shrink: 0;
    }}
    .stack-hide-btn svg,
    .stack-collapse-btn svg {{
      width: 14px;
      height: 14px;
    }}
    .stack-meta {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    .stack-name {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      font-weight: 600;
      letter-spacing: .04em;
      color: #c9d1de;
    }}
    .stack-name-icon {{
      display: inline-flex;
      align-items: center;
      color: #e3b341;
    }}
    .stack-name-icon.is-muted {{
      color: #8b94a3;
    }}
    .stack-name-icon svg {{
      width: 16px;
      height: 16px;
      display: block;
      fill: currentColor;
      stroke: currentColor;
    }}
    .hidden-stacks {{
      opacity: .42;
      margin-top: 8px;
      padding-top: 28px;
      border-top: 1px dashed #2a3544;
      transition: opacity .12s ease;
    }}
    .hidden-stacks:hover {{
      opacity: .58;
    }}
    .stack-block.is-collapsed .stack-head {{
      margin-bottom: 0;
    }}
    .stack-count {{
      font-size: 11px;
      font-weight: 600;
      color: #7d8695;
      background: #1e2530;
      border-radius: 20px;
      padding: 2px 9px;
    }}
    .row {{
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(0, 0.9fr) minmax(0, 0.85fr) minmax(0, 0.7fr) 72px;
      align-items: center;
      gap: 12px;
      padding: 14px 20px;
      background: #11151d;
      border: 1px solid #1c232e;
      border-radius: 12px;
      margin-bottom: 8px;
      transition: background .12s ease;
    }}
    .row:hover {{ background: #151b24; }}
    .row-name {{
      display: flex;
      align-items: center;
      gap: 11px;
      min-width: 0;
    }}
    .status-dot {{
      width: 9px;
      height: 9px;
      border-radius: 50%;
      flex-shrink: 0;
    }}
    .name-text {{
      font-size: 14px;
      font-weight: 600;
      white-space: nowrap;
      overflow: visible;
    }}
    body.truncate-names .name-text {{
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .image-text {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 12.5px;
      color: #8b94a3;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .status-cell {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .status-pill {{
      font-size: 12px;
      font-weight: 600;
      border-radius: 20px;
      padding: 3px 10px;
      white-space: nowrap;
      color: #8b94a3;
      background: rgba(139,148,163,.1);
    }}
    .ports {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .port-link {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      font-weight: 600;
      color: #6cb6ff;
      background: rgba(56,139,253,.12);
      border: 1px solid rgba(56,139,253,.25);
      border-radius: 7px;
      padding: 3px 9px;
      text-decoration: none;
      transition: background .12s;
    }}
    .port-link:hover {{ background: rgba(56,139,253,.22); }}
    .no-ports {{ font-size: 13px; color: #4c5566; }}
    .row-actions {{
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 2px;
    }}
    .logs-btn,
    .fav-btn,
    .hide-btn,
    .stack-collapse-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      padding: 0;
      color: #8b94a3;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 8px;
      cursor: pointer;
      transition: color .12s, background .12s, border-color .12s;
    }}
    .logs-btn:hover,
    .fav-btn:hover,
    .hide-btn:hover,
    .stack-collapse-btn:hover {{
      color: #e6e9ef;
      background: #1a2230;
      border-color: #2a3544;
    }}
    .fav-btn.is-on {{
      color: #e3b341;
    }}
    .fav-btn.is-on:hover {{
      color: #f0c14d;
    }}
    .hide-btn.is-on {{
      color: #7d8695;
    }}
    .hide-btn.is-on:hover {{
      color: #a3adbd;
    }}
    .logs-btn svg,
    .fav-btn svg,
    .hide-btn svg,
    .stack-collapse-btn svg {{
      width: 16px;
      height: 16px;
      display: block;
    }}
    .fav-btn.is-on svg {{
      fill: currentColor;
      stroke: currentColor;
    }}
    .hide-btn.is-on svg {{
      fill: currentColor;
      stroke: currentColor;
    }}
    .hidden-toggle-wrap {{
      margin-top: 4px;
      text-align: center;
    }}
    .hidden-toggle {{
      font-family: 'Inter', system-ui, sans-serif;
      font-size: 13px;
      font-weight: 500;
      color: #8b94a3;
      background: transparent;
      border: 1px dashed #2a3544;
      border-radius: 10px;
      padding: 10px 16px;
      cursor: pointer;
      transition: color .12s, border-color .12s, background .12s;
    }}
    .hidden-toggle:hover {{
      color: #c9d1de;
      background: #11151d;
      border-color: #3a4658;
    }}
    .empty {{
      text-align: center;
      padding: 60px 20px;
      color: #5b6472;
      font-size: 14px;
    }}
    .error {{
      background: rgba(248,81,73,.1);
      border: 1px solid rgba(248,81,73,.28);
      color: #f85149;
      border-radius: 12px;
      padding: 14px 18px;
      margin-bottom: 24px;
      font-size: 14px;
    }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      z-index: 50;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(6, 8, 12, .72);
      backdrop-filter: blur(6px);
    }}
    .modal-backdrop[hidden] {{ display: none; }}
    .modal {{
      width: min(920px, 100%);
      height: min(78vh, 760px);
      display: flex;
      flex-direction: column;
      background: #0f131a;
      border: 1px solid #243041;
      border-radius: 14px;
      box-shadow: 0 24px 80px rgba(0,0,0,.45);
      overflow: hidden;
    }}
    .modal.modal-sm {{
      width: min(420px, 100%);
      height: auto;
      max-height: min(70vh, 480px);
    }}
    .modal-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid #1e2530;
      background: #121820;
    }}
    .modal-title-wrap {{
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .modal-label {{
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: #7d8695;
    }}
    .modal-title {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 14px;
      font-weight: 600;
      color: #e6e9ef;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .modal-meta {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }}
    .live-badge {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 12px;
      font-weight: 600;
      color: #56d364;
      background: rgba(63,185,80,.1);
      border: 1px solid rgba(63,185,80,.22);
      border-radius: 20px;
      padding: 4px 10px;
    }}
    .live-badge.is-idle {{
      color: #8b94a3;
      background: rgba(139,148,163,.08);
      border-color: rgba(139,148,163,.18);
    }}
    .live-badge .live-dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
      animation: pulse 1.6s ease-in-out infinite;
    }}
    .live-badge.is-idle .live-dot {{ animation: none; opacity: .55; }}
    .modal-icon-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      padding: 0;
      border: 1px solid #2a3544;
      border-radius: 8px;
      background: #141922;
      color: #c9d1de;
      cursor: pointer;
      transition: color .12s, background .12s, border-color .12s;
    }}
    .modal-icon-btn:hover {{ background: #1a2230; color: #fff; }}
    .modal-icon-btn.is-copied {{
      color: #56d364;
      border-color: rgba(63,185,80,.35);
      background: rgba(63,185,80,.1);
    }}
    .modal-icon-btn svg {{
      width: 15px;
      height: 15px;
      display: block;
    }}
    .modal-close {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 34px;
      height: 34px;
      border: 1px solid #2a3544;
      border-radius: 8px;
      background: #141922;
      color: #c9d1de;
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
    }}
    .modal-close:hover {{ background: #1a2230; color: #fff; }}
    .modal-body {{
      flex: 1;
      min-height: 0;
      overflow: auto;
      background: #0f131a;
      scrollbar-width: thin;
      scrollbar-color: #2a3544 #0f131a;
    }}
    .modal-body::-webkit-scrollbar {{
      width: 10px;
    }}
    .modal-body::-webkit-scrollbar-track {{
      background: #0f131a;
    }}
    .modal-body::-webkit-scrollbar-thumb {{
      background: #2a3544;
      border-radius: 8px;
      border: 2px solid #0f131a;
    }}
    .modal-body::-webkit-scrollbar-thumb:hover {{
      background: #3a4658;
    }}
    .modal-scroll-progress {{
      height: 3px;
      flex-shrink: 0;
      background: #1e2530;
      overflow: hidden;
    }}
    .modal-scroll-progress[hidden] {{
      display: none;
    }}
    .modal-scroll-progress-bar {{
      height: 100%;
      width: 0%;
      background: #6cb6ff;
      transition: width .08s linear;
    }}
    .log-view {{
      margin: 0;
      padding: 16px 18px 28px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12.5px;
      line-height: 1.55;
      color: #c9d1de;
      white-space: pre-wrap;
      word-break: break-word;
      background: transparent;
    }}
    .log-view:empty::before {{
      content: "Aguardando logs…";
      color: #5b6472;
    }}
    .settings-body {{
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .settings-option {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      cursor: pointer;
    }}
    .settings-option input {{
      margin-top: 2px;
      accent-color: #6cb6ff;
      flex-shrink: 0;
    }}
    .settings-option-text {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .settings-option-label {{
      font-size: 14px;
      font-weight: 600;
      color: #e6e9ef;
    }}
    .settings-option-hint {{
      font-size: 12px;
      color: #7d8695;
      line-height: 1.4;
    }}
    body.compact-view .stack-block {{
      margin-bottom: 24px;
    }}
    body.compact-view .stack-head {{
      margin-bottom: 6px;
    }}
    body.compact-view .stack-body {{
      background: #11151d;
      border: 1px solid #1c232e;
      border-radius: 10px;
      overflow: hidden;
    }}
    body.compact-view .stack-body .row {{
      border: none;
      border-radius: 0;
      margin-bottom: 0;
      padding: 6px 12px;
      gap: 8px;
      border-bottom: 1px solid #1a2030;
    }}
    body.compact-view .stack-body .row:last-child {{
      border-bottom: none;
    }}
    body.compact-view .stack-block > .row {{
      padding: 6px 12px;
      margin-bottom: 4px;
      gap: 8px;
      border-radius: 8px;
    }}
    body.compact-view .row {{
      grid-template-columns: minmax(0, 1.4fr) minmax(0, 0.85fr) minmax(0, 0.8fr) minmax(0, 0.65fr) 60px;
    }}
    body.compact-view .status-dot {{
      width: 7px;
      height: 7px;
      box-shadow: none !important;
    }}
    body.compact-view .row-name {{
      gap: 8px;
    }}
    body.compact-view .name-text {{
      font-size: 12.5px;
    }}
    body.compact-view .image-text {{
      font-size: 11px;
    }}
    body.compact-view .status-pill {{
      font-size: 11px;
      padding: 2px 7px;
    }}
    body.compact-view .port-link {{
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 5px;
    }}
    body.compact-view .no-ports {{
      font-size: 12px;
    }}
    body.compact-view .logs-btn,
    body.compact-view .fav-btn,
    body.compact-view .hide-btn {{
      width: 28px;
      height: 28px;
      border-radius: 6px;
    }}
    body.compact-view .logs-btn svg,
    body.compact-view .fav-btn svg,
    body.compact-view .hide-btn svg {{
      width: 14px;
      height: 14px;
    }}
    @media (max-width: 960px) {{
      .page {{ padding: 28px 20px 48px; }}
      .meters {{ grid-template-columns: repeat(2, 1fr); }}
      .row {{
        grid-template-columns: 1fr auto;
        gap: 10px;
      }}
      .row-name {{ grid-column: 1; }}
      .row-actions {{ grid-column: 2; grid-row: 1; }}
      .image-text,
      .status-cell,
      .ports {{ grid-column: 1 / -1; }}
      body.compact-view .row {{
        gap: 6px;
      }}
    }}
    @media (max-width: 560px) {{
      .meters {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 24px; }}
      .modal {{ height: min(88vh, 760px); }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="wrap">
      <div class="header">
        <div>
          <div class="eyebrow">
            <div class="eyebrow-dot"></div>
            <span class="eyebrow-text">Docker · Homelab</span>
          </div>
          <h1>Containers ativos</h1>
        </div>
        <div class="clock">
          <span class="clock-dot"></span>
          Atualizado <span id="clock"></span>
          <button type="button" class="header-settings-btn" id="settings-open" title="Configurações" aria-label="Abrir configurações">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6"/><path d="m19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.26.604.852.997 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1"/></svg>
          </button>
        </div>
      </div>

      <div class="meters" id="meters"></div>

      <div id="error" class="error" hidden></div>

      <div class="toolbar">
        <div class="search-wrap">
          <span class="search-icon">⌕</span>
          <input id="q" class="search-input" type="search"
            placeholder="Filtrar por nome, imagem ou stack…" autocomplete="off">
        </div>
        <div class="sort">
          <span class="sort-label">Ordenar</span>
          <button type="button" class="sort-btn" data-key="status">Status</button>
          <button type="button" class="sort-btn" data-key="port">Porta</button>
          <button type="button" class="sort-btn" data-key="name">Nome</button>
        </div>
      </div>

      <div id="stacks"></div>
      <div id="hidden-toggle-wrap" class="hidden-toggle-wrap" hidden>
        <button type="button" class="hidden-toggle" id="hidden-toggle"></button>
      </div>
      <div id="hidden-stacks" class="hidden-stacks" hidden></div>
      <div id="empty" class="empty" hidden></div>
    </div>
  </div>

  <div id="storage-tooltip" class="storage-tooltip" hidden></div>

  <div id="settings-modal" class="modal-backdrop" hidden>
    <div class="modal modal-sm" role="dialog" aria-modal="true" aria-labelledby="settings-modal-title">
      <div class="modal-head">
        <div class="modal-title-wrap">
          <span class="modal-label">Configurações</span>
          <div class="modal-title" id="settings-modal-title">Preferências</div>
        </div>
        <button type="button" class="modal-close" id="settings-close" aria-label="Fechar">×</button>
      </div>
      <div class="modal-body settings-body">
        <label class="settings-option">
          <input type="checkbox" id="settings-compact-view">
          <span class="settings-option-text">
            <span class="settings-option-label">Visualização compacta</span>
            <span class="settings-option-hint">Linhas mais densas, com containers agrupados por stack</span>
          </span>
        </label>
        <label class="settings-option">
          <input type="checkbox" id="settings-truncate-names">
          <span class="settings-option-text">
            <span class="settings-option-label">Truncar nome do container</span>
            <span class="settings-option-hint">Exibe reticências quando o nome não couber na coluna</span>
          </span>
        </label>
      </div>
    </div>
  </div>

  <div id="logs-modal" class="modal-backdrop" hidden>
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="logs-modal-title">
      <div class="modal-head">
        <div class="modal-title-wrap">
          <span class="modal-label">Logs</span>
          <div class="modal-title" id="logs-modal-title"></div>
        </div>
        <div class="modal-meta">
          <span id="logs-live" class="live-badge is-idle">
            <span class="live-dot"></span>
            <span id="logs-live-text">Parado</span>
          </span>
          <button type="button" class="modal-icon-btn" id="logs-copy" title="Copiar logs" aria-label="Copiar logs">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
          <button type="button" class="modal-close" id="logs-close" aria-label="Fechar">×</button>
        </div>
      </div>
      <div class="modal-scroll-progress" id="logs-progress" hidden>
        <div class="modal-scroll-progress-bar" id="logs-progress-bar"></div>
      </div>
      <div class="modal-body" id="logs-scroll">
        <pre class="log-view" id="logs-view"></pre>
      </div>
    </div>
  </div>

  <script>
    let DATA = {data_json};
    const APP_BUILD = DATA.loaded_build;

    const state = {{ query: "", sortKey: null, sortDir: 1, showHidden: false }};
    const REFRESH_MS = 5000;
    let refreshing = false;

    const LOGS_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h8M8 10h8M8 14h5"/><rect x="4" y="3" width="16" height="18" rx="2"/></svg>`;
    const STAR_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3.5 2.7 5.5 6 .9-4.4 4.2 1 5.9L12 17.2 6.7 20l1-5.9L3.4 9.9l6-.9z"/></svg>`;
    const HIDE_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
    const CHEVRON_DOWN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`;
    const CHEVRON_UP = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m18 15-6-6-6 6"/></svg>`;
    const FAV_KEY = "homelab-homepage-favorites";
    const HIDDEN_KEY = "homelab-homepage-hidden";
    const HIDDEN_STACKS_KEY = "homelab-homepage-hidden-stacks";
    const COLLAPSED_KEY = "homelab-homepage-collapsed-stacks";
    const SETTINGS_KEY = "homelab-homepage-settings";

    let favorites = loadFavorites();
    let hiddenContainers = loadHidden();
    let hiddenStacks = loadHiddenStacks();
    let collapsedStacks = loadCollapsedStacks();
    let settings = loadSettings();
    let logsAbort = null;
    let logsStickBottom = true;
    let copyResetTimer = null;

    function loadFavorites() {{
      try {{
        const raw = localStorage.getItem(FAV_KEY);
        const list = raw ? JSON.parse(raw) : [];
        return new Set(Array.isArray(list) ? list.map(String) : []);
      }} catch {{
        return new Set();
      }}
    }}

    function saveFavorites() {{
      localStorage.setItem(FAV_KEY, JSON.stringify([...favorites]));
    }}

    function isFavorite(name) {{
      return favorites.has(name);
    }}

    function toggleFavorite(name) {{
      if (favorites.has(name)) favorites.delete(name);
      else favorites.add(name);
      saveFavorites();
      render();
    }}

    function loadHidden() {{
      try {{
        const raw = localStorage.getItem(HIDDEN_KEY);
        const list = raw ? JSON.parse(raw) : [];
        return new Set(Array.isArray(list) ? list.map(String) : []);
      }} catch {{
        return new Set();
      }}
    }}

    function loadHiddenStacks() {{
      try {{
        const raw = localStorage.getItem(HIDDEN_STACKS_KEY);
        const list = raw ? JSON.parse(raw) : [];
        return new Set(Array.isArray(list) ? list.map(String) : []);
      }} catch {{
        return new Set();
      }}
    }}

    function saveHidden() {{
      localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenContainers]));
      localStorage.setItem(HIDDEN_STACKS_KEY, JSON.stringify([...hiddenStacks]));
    }}

    function isHiddenStack(stackName) {{
      return hiddenStacks.has(stackName);
    }}

    function isHiddenContainer(c) {{
      return hiddenStacks.has(c.stack) || hiddenContainers.has(c.name);
    }}

    function isHidden(name) {{
      const container = DATA.containers.find((c) => c.name === name);
      return container ? isHiddenContainer(container) : hiddenContainers.has(name);
    }}

    function containerNamesInStack(stackName) {{
      return DATA.containers
        .filter((c) => c.stack === stackName)
        .map((c) => c.name);
    }}

    function toggleHiddenStack(stackName) {{
      if (hiddenStacks.has(stackName)) {{
        hiddenStacks.delete(stackName);
      }} else {{
        hiddenStacks.add(stackName);
        containerNamesInStack(stackName).forEach((name) => hiddenContainers.delete(name));
      }}
      saveHidden();
      if (!hiddenContainers.size && !hiddenStacks.size) state.showHidden = false;
      render();
    }}

    function toggleHidden(name) {{
      const container = DATA.containers.find((c) => c.name === name);
      if (!container) return;
      const stack = container.stack;

      if (hiddenStacks.has(stack)) {{
        hiddenStacks.delete(stack);
        DATA.containers
          .filter((c) => c.stack === stack && c.name !== name)
          .forEach((c) => hiddenContainers.add(c.name));
      }} else if (hiddenContainers.has(name)) {{
        hiddenContainers.delete(name);
      }} else {{
        hiddenContainers.add(name);
      }}

      saveHidden();
      if (!hiddenContainers.size && !hiddenStacks.size) state.showHidden = false;
      render();
    }}

    function toggleShowHidden() {{
      state.showHidden = !state.showHidden;
      render();
    }}

    function loadCollapsedStacks() {{
      try {{
        const raw = localStorage.getItem(COLLAPSED_KEY);
        const list = raw ? JSON.parse(raw) : [];
        return new Set(Array.isArray(list) ? list.map(String) : []);
      }} catch {{
        return new Set();
      }}
    }}

    function saveCollapsedStacks() {{
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsedStacks]));
    }}

    function stackKey(stack) {{
      return stack.isFavorites ? "__favorites__" : stack.name;
    }}

    function isStackCollapsed(stack) {{
      return collapsedStacks.has(stackKey(stack));
    }}

    function toggleStackCollapsed(stackId) {{
      if (collapsedStacks.has(stackId)) collapsedStacks.delete(stackId);
      else collapsedStacks.add(stackId);
      saveCollapsedStacks();
      render();
    }}

    function loadSettings() {{
      try {{
        const raw = localStorage.getItem(SETTINGS_KEY);
        const data = raw ? JSON.parse(raw) : {{}};
        return {{
          compactView: Boolean(data.compactView),
          truncateNames: Boolean(data.truncateNames),
        }};
      }} catch {{
        return {{ compactView: false, truncateNames: false }};
      }}
    }}

    function saveSettings() {{
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    }}

    function applySettings() {{
      document.body.classList.toggle("compact-view", settings.compactView);
      document.body.classList.toggle("truncate-names", settings.truncateNames);
      const compactCheckbox = document.getElementById("settings-compact-view");
      if (compactCheckbox) compactCheckbox.checked = settings.compactView;
      const truncateCheckbox = document.getElementById("settings-truncate-names");
      if (truncateCheckbox) truncateCheckbox.checked = settings.truncateNames;
    }}

    function openSettings() {{
      applySettings();
      document.getElementById("settings-modal").hidden = false;
      document.body.style.overflow = "hidden";
    }}

    function closeSettings() {{
      document.getElementById("settings-modal").hidden = true;
      if (document.getElementById("logs-modal").hidden) {{
        document.body.style.overflow = "";
      }}
    }}

    function setCompactView(enabled) {{
      settings.compactView = enabled;
      saveSettings();
      applySettings();
    }}

    function setTruncateNames(enabled) {{
      settings.truncateNames = enabled;
      saveSettings();
      applySettings();
    }}


    const COPY_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
    const CHECK_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>`;

    function now() {{
      return new Date().toLocaleTimeString("pt-BR", {{
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      }});
    }}

    function setClock() {{
      document.getElementById("clock").textContent = now();
    }}

    function uptimeSeconds(status) {{
      const m = status.match(/up\\s+(\\d+)\\s+(second|minute|hour|day|week|month)/i);
      if (!m) return 0;
      const mult = {{ second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2592000 }};
      return parseInt(m[1], 10) * (mult[m[2].toLowerCase()] || 1);
    }}

    function dotStyle(status) {{
      const s = status.toLowerCase();
      if (s.includes("second") || s.includes("minute")) {{
        return {{
          dot: "#d29922", glow: "rgba(210,153,34,.18)",
        }};
      }}
      return {{
        dot: "#3fb950", glow: "rgba(63,185,80,.18)",
      }};
    }}

    function healthStyle(health) {{
      if (health === "healthy") {{
        return {{ color: "#56d364", bg: "rgba(63,185,80,.10)" }};
      }}
      if (health === "starting") {{
        return {{ color: "#e3b341", bg: "rgba(210,153,34,.12)" }};
      }}
      if (health === "unhealthy") {{
        return {{ color: "#f85149", bg: "rgba(248,81,73,.12)" }};
      }}
      return {{ color: "#8b94a3", bg: "rgba(139,148,163,.1)" }};
    }}

    function decorate(c) {{
      const dot = dotStyle(c.status);
      const health = healthStyle(c.health);
      return {{
        ...c,
        dotColor: dot.dot,
        dotGlow: dot.glow,
        statusColor: health.color,
        statusBg: health.bg,
        noPorts: !c.ports || c.ports.length === 0,
      }};
    }}

    function portHref(port) {{
      const scheme = [443, 8443, 9443].includes(port) ? "https" : "http";
      return scheme + "://" + DATA.host + ":" + port;
    }}

    function filteredRows() {{
      const q = state.query.trim().toLowerCase();
      return DATA.containers.filter((c) =>
        !q ||
        c.name.toLowerCase().includes(q) ||
        c.image.toLowerCase().includes(q) ||
        c.stack.toLowerCase().includes(q)
      );
    }}

    function isEmptyStack(name) {{
      return name === "sem stack";
    }}

    function compareRows(a, b, sortKey, sortDir) {{
      if (!sortKey) return a.name.localeCompare(b.name);
      let r = 0;
      if (sortKey === "name") r = a.name.localeCompare(b.name);
      else if (sortKey === "port") r = (a.ports[0] ?? Infinity) - (b.ports[0] ?? Infinity);
      else if (sortKey === "status") r = uptimeSeconds(a.status) - uptimeSeconds(b.status);
      return r * sortDir;
    }}

    function compareStacks(a, b, sortKey, sortDir, map) {{
      const aEmpty = isEmptyStack(a);
      const bEmpty = isEmptyStack(b);
      if (aEmpty !== bEmpty) return aEmpty ? 1 : -1;
      if (sortKey) {{
        const ca = map[a];
        const cb = map[b];
        if (ca.length && cb.length) {{
          const r = compareRows(ca[0], cb[0], sortKey, sortDir);
          if (r !== 0) return r;
        }}
      }}
      return a.localeCompare(b);
    }}

    function groupRowsIntoStacks(rows, {{ withFavorites = false }} = {{}}) {{
      const {{ sortKey, sortDir }} = state;
      const favRows = [];
      const restRows = [];
      rows.forEach((c) => {{
        if (withFavorites && isFavorite(c.name)) favRows.push(c);
        else restRows.push(c);
      }});

      const order = [];
      const map = {{}};
      restRows.forEach((c) => {{
        if (!map[c.stack]) {{ map[c.stack] = []; order.push(c.stack); }}
        map[c.stack].push(c);
      }});
      for (const name of order) {{
        map[name].sort((a, b) => compareRows(a, b, sortKey, sortDir));
      }}
      order.sort((a, b) => compareStacks(a, b, sortKey, sortDir, map));

      const stacks = order.map((name) => ({{
        name,
        count: map[name].length,
        containers: map[name].map(decorate),
        showTitle: !isEmptyStack(name),
        isFavorites: false,
      }}));

      if (favRows.length) {{
        favRows.sort((a, b) => compareRows(a, b, sortKey, sortDir));
        stacks.unshift({{
          name: "Favoritos",
          count: favRows.length,
          containers: favRows.map(decorate),
          showTitle: true,
          isFavorites: true,
        }});
      }}

      return stacks;
    }}

    function buildLists(rows) {{
      const visibleRows = rows.filter((c) => !isHiddenContainer(c));
      const hiddenRows = rows.filter((c) => isHiddenContainer(c));
      return {{
        visibleStacks: groupRowsIntoStacks(visibleRows, {{ withFavorites: true }}),
        hiddenStacks: groupRowsIntoStacks(hiddenRows, {{ withFavorites: false }}),
        hiddenCount: hiddenRows.length,
      }};
    }}

    function esc(s) {{
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function formatStorageGb(gb) {{
      return (gb >= 100 ? gb.toFixed(0) : gb.toFixed(1)) + " GB";
    }}

    function renderStorageBar(row) {{
      return `
        <div class="storage-row" data-used-gb="${{esc(String(row.used_gb))}}" data-total-gb="${{esc(String(row.total_gb))}}">
          <span class="storage-label" title="${{esc(row.path)}}">${{esc(row.label)}}</span>
          <div class="storage-bar">
            <span style="width:${{esc(String(row.pct))}}%;background:${{esc(row.color)}}"></span>
          </div>
          <span class="storage-pct">${{esc(String(row.pct))}}%</span>
        </div>
      `;
    }}

    const storageTooltip = document.getElementById("storage-tooltip");
    let activeStorageRow = null;

    function showStorageTooltip(row, x, y) {{
      const used = Number(row.dataset.usedGb);
      const total = Number(row.dataset.totalGb);
      if (!Number.isFinite(used) || !Number.isFinite(total)) return;
      storageTooltip.textContent = `${{formatStorageGb(used)}} usados · ${{formatStorageGb(total)}} disponíveis`;
      storageTooltip.hidden = false;
      positionStorageTooltip(x, y);
    }}

    function positionStorageTooltip(x, y) {{
      storageTooltip.style.left = x + "px";
      storageTooltip.style.top = y + "px";
    }}

    function hideStorageTooltip() {{
      activeStorageRow = null;
      storageTooltip.hidden = true;
    }}

    function renderMeter(m) {{
      if (m.type === "storage") {{
        const bars = [renderStorageBar(m.main)]
          .concat((m.mounts || []).map(renderStorageBar))
          .join("");
        return `
          <div class="meter storage">
            <div class="meter-top">
              <span class="meter-label">${{esc(m.label)}}</span>
              ${{m.showSub ? `<span class="meter-sub">${{esc(m.sub)}}</span>` : ""}}
            </div>
            <div class="meter-value" style="color:${{esc(m.color)}};margin-top:6px">${{esc(m.display)}}</div>
            <div class="storage-bars">${{bars}}</div>
          </div>
        `;
      }}

      return `
        <div class="meter">
          <div class="meter-top">
            <span class="meter-label">${{esc(m.label)}}</span>
            ${{m.showSub ? `<span class="meter-sub">${{esc(m.sub)}}</span>` : ""}}
          </div>
          ${{m.showBar ? `
            <div class="meter-value-row">
              <div class="meter-value" style="color:${{esc(m.color)}}">${{esc(m.display)}}</div>
              <div class="meter-bar"><span style="width:${{esc(m.barWidth)}};background:${{esc(m.color)}}"></span></div>
            </div>
          ` : `<div class="meter-value" style="color:${{esc(m.color)}};margin-top:6px">${{esc(m.display)}}</div>`}}
          ${{m.showChart ? `
            <div class="meter-chart-wrap">
              <div class="meter-chart-axis" aria-hidden="true">
                <span>100</span>
                <span>0</span>
              </div>
              <svg class="meter-chart" data-chart="${{esc(m.chartKey)}}" viewBox="0 0 200 48" preserveAspectRatio="none" role="img" aria-label="Histórico de uso"></svg>
              <div class="meter-chart-times" data-chart-times="${{esc(m.chartKey)}}" aria-hidden="true">
                <span></span>
                <span></span>
              </div>
            </div>
          ` : ""}}
          ${{m.showCaption ? `<div class="meter-caption">${{esc(m.caption)}}</div>` : ""}}
        </div>
      `;
    }}

    const CHART_HISTORY_MAX = 60;
    const usageHistory = {{ cpu: [], ram: [] }};

    function recordUsageSamples() {{
      for (const m of DATA.meters) {{
        if (!m.chartKey) continue;
        const bucket = usageHistory[m.chartKey];
        if (!bucket) continue;
        bucket.push({{ t: Date.now(), v: m.pct ?? 0 }});
        if (bucket.length > CHART_HISTORY_MAX) bucket.shift();
      }}
      drawUsageCharts();
    }}

    function formatChartTime(ts) {{
      return new Date(ts).toLocaleTimeString("pt-BR", {{
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      }});
    }}

    function drawUsageChart(svg, points, color) {{
      const W = 200;
      const H = 48;

      if (!points.length) {{
        svg.innerHTML = "";
        return;
      }}

      const tMin = points[0].t;
      const tMax = points[points.length - 1].t;
      const tSpan = Math.max(tMax - tMin, 1);

      const xAt = (t) => ((t - tMin) / tSpan) * W;
      const yAt = (v) => {{
        const clamped = Math.max(0, Math.min(100, v));
        return H - (clamped / 100) * H;
      }};

      const coords = points.map((p) => [xAt(p.t), yAt(p.v)]);
      const linePts = coords.map(([x, y]) => `${{x.toFixed(1)}},${{y.toFixed(1)}}`).join(" ");
      const firstX = coords[0][0].toFixed(1);
      const lastX = coords[coords.length - 1][0].toFixed(1);
      const baseY = H.toFixed(1);
      const areaPts = `${{firstX}},${{baseY}} ${{linePts}} ${{lastX}},${{baseY}}`;
      const y50 = yAt(50).toFixed(1);

      svg.innerHTML = `
        <line x1="0" y1="${{y50}}" x2="${{W}}" y2="${{y50}}" stroke="#1e2530" stroke-width="1"/>
        <polygon points="${{areaPts}}" fill="${{esc(color)}}" fill-opacity="0.14"/>
        <polyline points="${{linePts}}" fill="none" stroke="${{esc(color)}}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
      `;

      const wrap = svg.closest(".meter-chart-wrap");
      const times = wrap ? wrap.querySelector(".meter-chart-times") : null;
      if (times) {{
        const spans = times.querySelectorAll("span");
        if (spans[0]) spans[0].textContent = formatChartTime(tMin);
        if (spans[1]) spans[1].textContent = formatChartTime(tMax);
      }}
    }}

    function drawUsageCharts() {{
      document.querySelectorAll(".meter-chart").forEach((svg) => {{
        const key = svg.dataset.chart;
        const meter = DATA.meters.find((m) => m.chartKey === key);
        const color = meter ? meter.color : "#6cb6ff";
        drawUsageChart(svg, usageHistory[key] || [], color);
      }});
    }}

    function renderMeters() {{
      const el = document.getElementById("meters");
      el.innerHTML = DATA.meters.map(renderMeter).join("");
    }}

    function renderContainerRow(c) {{
      const hidden = isHidden(c.name);
      return `
        <div class="row">
          <div class="row-name">
            <span class="status-dot" style="background:${{c.dotColor}};box-shadow:0 0 0 3px ${{c.dotGlow}};"></span>
            <span class="name-text" title="${{esc(c.name)}}">${{esc(c.name)}}</span>
          </div>
          <div class="image-text" title="${{esc(c.image)}}">${{esc(c.image)}}</div>
          <div class="status-cell">
            <span class="status-pill" style="color:${{c.statusColor}};background:${{c.statusBg}}">${{esc(c.status)}}</span>
          </div>
          <div class="ports">
            ${{c.noPorts
              ? `<span class="no-ports">—</span>`
              : c.ports.map((p) =>
                  `<a class="port-link" href="${{esc(portHref(p))}}" target="_blank" rel="noopener">:${{p}}</a>`
                ).join("")
            }}
          </div>
          <div class="row-actions">
            <button type="button" class="fav-btn${{isFavorite(c.name) ? " is-on" : ""}}" data-fav="${{esc(c.name)}}" title="${{isFavorite(c.name) ? "Remover dos favoritos" : "Favoritar"}}" aria-label="${{isFavorite(c.name) ? "Remover " + esc(c.name) + " dos favoritos" : "Favoritar " + esc(c.name)}}" aria-pressed="${{isFavorite(c.name) ? "true" : "false"}}">${{STAR_ICON}}</button>
            <button type="button" class="hide-btn${{hidden ? " is-on" : ""}}" data-hide="${{esc(c.name)}}" title="${{hidden ? "Mostrar container" : "Esconder container"}}" aria-label="${{hidden ? "Mostrar " + esc(c.name) : "Esconder " + esc(c.name)}}" aria-pressed="${{hidden ? "true" : "false"}}">${{HIDE_ICON}}</button>
            <button type="button" class="logs-btn" data-logs="${{esc(c.id)}}" data-name="${{esc(c.name)}}" title="Ver logs" aria-label="Ver logs de ${{esc(c.name)}}">${{LOGS_ICON}}</button>
          </div>
        </div>
      `;
    }}

    function renderStackHead(stack, {{ showStackHide = false }} = {{}}) {{
      if (!stack.showTitle) return "";
      const stackHidden = !stack.isFavorites && isHiddenStack(stack.name);
      const collapsed = isStackCollapsed(stack);
      const key = stackKey(stack);
      return `
        <div class="stack-head">
          <span class="stack-name${{stack.isFavorites ? " stack-name-icon" : ""}}" title="${{stack.isFavorites ? "Favoritos" : esc(stack.name)}}" aria-label="${{stack.isFavorites ? "Favoritos" : esc(stack.name)}}">${{stack.isFavorites ? STAR_ICON : esc(stack.name)}}</span>
          <div class="stack-meta">
            <span class="stack-count">${{stack.count}}</span>
            <button type="button" class="stack-collapse-btn" data-collapse-stack="${{esc(key)}}" title="${{collapsed ? "Expandir stack" : "Comprimir stack"}}" aria-label="${{collapsed ? "Expandir stack " + esc(stack.name) : "Comprimir stack " + esc(stack.name)}}" aria-expanded="${{collapsed ? "false" : "true"}}">${{collapsed ? CHEVRON_DOWN : CHEVRON_UP}}</button>
            ${{showStackHide && !stack.isFavorites ? `
              <button type="button" class="hide-btn stack-hide-btn${{stackHidden ? " is-on" : ""}}" data-hide-stack="${{esc(stack.name)}}" title="${{stackHidden ? "Mostrar stack" : "Esconder stack"}}" aria-label="${{stackHidden ? "Mostrar stack " + esc(stack.name) : "Esconder stack " + esc(stack.name)}}" aria-pressed="${{stackHidden ? "true" : "false"}}">${{HIDE_ICON}}</button>
            ` : ""}}
          </div>
        </div>
      `;
    }}

    function renderStackBlock(stack, {{ showStackHide = false }} = {{}}) {{
      const collapsed = stack.showTitle && isStackCollapsed(stack);
      const rows = stack.containers.map((c) => renderContainerRow(c)).join("");
      return `
        <div class="stack-block${{collapsed ? " is-collapsed" : ""}}">
          ${{renderStackHead(stack, {{ showStackHide }})}}
          ${{stack.showTitle
            ? `<div class="stack-body"${{collapsed ? " hidden" : ""}}>${{rows}}</div>`
            : rows}}
        </div>
      `;
    }}

    function renderStacksHtml(stacks, {{ showStackHide = false }} = {{}}) {{
      return stacks.map((stack) => renderStackBlock(stack, {{ showStackHide }})).join("");
    }}

    function renderHiddenToggle(hiddenCount) {{
      const wrap = document.getElementById("hidden-toggle-wrap");
      const btn = document.getElementById("hidden-toggle");
      if (!hiddenCount) {{
        wrap.hidden = true;
        return;
      }}
      wrap.hidden = false;
      btn.textContent = state.showHidden
        ? "Ocultar containers escondidos"
        : `Mostrar containers escondidos (${{hiddenCount}})`;
      btn.setAttribute(
        "aria-label",
        state.showHidden ? "Ocultar containers escondidos" : `Mostrar ${{hiddenCount}} containers escondidos`
      );
    }}

    function renderSort() {{
      document.querySelectorAll(".sort-btn").forEach((btn) => {{
        const active = state.sortKey === btn.dataset.key;
        btn.classList.toggle("active", active);
        const arrow = btn.querySelector(".arrow");
        if (arrow) arrow.remove();
        if (active) {{
          const span = document.createElement("span");
          span.className = "arrow";
          span.textContent = state.sortDir === 1 ? "▲" : "▼";
          btn.appendChild(span);
        }}
      }});
    }}

    function renderStacks() {{
      const rows = filteredRows();
      const {{ visibleStacks, hiddenStacks: hiddenStacksList, hiddenCount }} = buildLists(rows);
      const root = document.getElementById("stacks");
      const hiddenRoot = document.getElementById("hidden-stacks");
      const empty = document.getElementById("empty");

      const err = document.getElementById("error");
      if (DATA.error) {{
        err.hidden = false;
        err.textContent = DATA.error;
      }} else {{
        err.hidden = true;
        err.textContent = "";
      }}

      renderHiddenToggle(hiddenCount);

      const hasVisibleStacks = visibleStacks.length > 0;
      const showHiddenList = state.showHidden && hiddenStacksList.length > 0;

      if (!hasVisibleStacks && !showHiddenList) {{
        root.innerHTML = "";
        hiddenRoot.hidden = true;
        hiddenRoot.innerHTML = "";
        empty.hidden = !(rows.length === 0);
        if (!empty.hidden) {{
          empty.textContent = state.query.trim()
            ? `Nenhum container corresponde a "${{state.query}}".`
            : "Nenhum container ativo.";
        }}
        return;
      }}

      empty.hidden = true;
      root.innerHTML = hasVisibleStacks
        ? renderStacksHtml(visibleStacks, {{ showStackHide: true }})
        : "";

      if (showHiddenList) {{
        hiddenRoot.hidden = false;
        hiddenRoot.innerHTML = renderStacksHtml(hiddenStacksList, {{ showStackHide: true }});
      }} else {{
        hiddenRoot.hidden = true;
        hiddenRoot.innerHTML = "";
      }}
    }}

    function render() {{
      renderSort();
      renderStacks();
    }}

    function toggleSort(key) {{
      if (state.sortKey === key) {{
        if (state.sortDir === 1) state.sortDir = -1;
        else {{ state.sortKey = null; state.sortDir = 1; }}
      }} else {{
        state.sortKey = key;
        state.sortDir = 1;
      }}
      render();
    }}

    async function refresh() {{
      if (refreshing || document.hidden) return;
      refreshing = true;
      try {{
        const res = await fetch("/api/status", {{ cache: "no-store" }});
        if (!res.ok) throw new Error("HTTP " + res.status);
        DATA = await res.json();
        if (DATA.build && DATA.build !== APP_BUILD) {{
          location.reload();
          return;
        }}
        setClock();
        renderMeters();
        recordUsageSamples();
        render();
      }} catch (e) {{
        const err = document.getElementById("error");
        err.hidden = false;
        err.textContent = "Falha ao atualizar: " + (e && e.message ? e.message : e);
      }} finally {{
        refreshing = false;
      }}
    }}

    function setLogsLive(live) {{
      const badge = document.getElementById("logs-live");
      const text = document.getElementById("logs-live-text");
      badge.classList.toggle("is-idle", !live);
      text.textContent = live ? "Ao vivo" : "Parado";
    }}

    function resetCopyBtn() {{
      const btn = document.getElementById("logs-copy");
      if (copyResetTimer) {{
        clearTimeout(copyResetTimer);
        copyResetTimer = null;
      }}
      btn.classList.remove("is-copied");
      btn.innerHTML = COPY_ICON;
      btn.title = "Copiar logs";
      btn.setAttribute("aria-label", "Copiar logs");
    }}

    function closeLogs() {{
      if (logsAbort) {{
        logsAbort.abort();
        logsAbort = null;
      }}
      setLogsLive(false);
      resetCopyBtn();
      document.getElementById("logs-progress").hidden = true;
      document.getElementById("logs-progress-bar").style.width = "0%";
      document.getElementById("logs-modal").hidden = true;
      if (document.getElementById("settings-modal").hidden) {{
        document.body.style.overflow = "";
      }}
    }}

    function appendLogs(text) {{
      const view = document.getElementById("logs-view");
      const scroller = document.getElementById("logs-scroll");
      view.textContent += text;
      if (logsStickBottom) scroller.scrollTop = scroller.scrollHeight;
      updateScrollProgress();
    }}

    function updateScrollProgress() {{
      const el = document.getElementById("logs-scroll");
      const bar = document.getElementById("logs-progress");
      const fill = document.getElementById("logs-progress-bar");
      const max = el.scrollHeight - el.clientHeight;
      const atBottom = max <= 0 || el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      logsStickBottom = atBottom;
      if (atBottom) {{
        bar.hidden = true;
        fill.style.width = "0%";
        return;
      }}
      bar.hidden = false;
      const pct = Math.max(0, Math.min(100, (el.scrollTop / max) * 100));
      fill.style.width = pct.toFixed(2) + "%";
    }}

    async function openLogs(id, name) {{
      closeLogs();
      const modal = document.getElementById("logs-modal");
      const view = document.getElementById("logs-view");
      const scroller = document.getElementById("logs-scroll");
      document.getElementById("logs-modal-title").textContent = name;
      view.textContent = "";
      logsStickBottom = true;
      document.getElementById("logs-progress").hidden = true;
      document.getElementById("logs-progress-bar").style.width = "0%";
      modal.hidden = false;
      document.body.style.overflow = "hidden";
      setLogsLive(true);

      const ctrl = new AbortController();
      logsAbort = ctrl;
      try {{
        const res = await fetch("/api/logs/" + encodeURIComponent(id), {{
          cache: "no-store",
          signal: ctrl.signal,
        }});
        if (!res.ok) {{
          const msg = await res.text();
          throw new Error(msg || ("HTTP " + res.status));
        }}
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        while (true) {{
          const {{ done, value }} = await reader.read();
          if (done) break;
          appendLogs(decoder.decode(value, {{ stream: true }}));
        }}
        appendLogs(decoder.decode());
      }} catch (e) {{
        if (e && e.name === "AbortError") return;
        appendLogs("\\n[erro] " + (e && e.message ? e.message : e) + "\\n");
      }} finally {{
        if (logsAbort === ctrl) {{
          logsAbort = null;
          setLogsLive(false);
        }}
      }}
    }}

    async function copyLogs() {{
      const text = document.getElementById("logs-view").textContent || "";
      if (!text) return;
      const btn = document.getElementById("logs-copy");
      try {{
        await navigator.clipboard.writeText(text);
      }} catch (e) {{
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }}
      btn.classList.add("is-copied");
      btn.innerHTML = CHECK_ICON;
      btn.title = "Copiado";
      btn.setAttribute("aria-label", "Logs copiados");
      if (copyResetTimer) clearTimeout(copyResetTimer);
      copyResetTimer = setTimeout(() => {{
        btn.classList.remove("is-copied");
        btn.innerHTML = COPY_ICON;
        btn.title = "Copiar logs";
        btn.setAttribute("aria-label", "Copiar logs");
      }}, 1600);
    }}

    document.getElementById("q").addEventListener("input", (e) => {{
      state.query = e.target.value;
      render();
    }});

    document.querySelectorAll(".sort-btn").forEach((btn) => {{
      btn.addEventListener("click", () => toggleSort(btn.dataset.key));
    }});

    document.getElementById("meters").addEventListener("pointerover", (e) => {{
      const row = e.target.closest(".storage-row");
      if (!row) return;
      activeStorageRow = row;
      showStorageTooltip(row, e.clientX, e.clientY);
    }});

    document.getElementById("meters").addEventListener("pointermove", (e) => {{
      if (!activeStorageRow || storageTooltip.hidden) return;
      const row = e.target.closest(".storage-row");
      if (row !== activeStorageRow) return;
      positionStorageTooltip(e.clientX, e.clientY);
    }});

    document.getElementById("meters").addEventListener("pointerout", (e) => {{
      const row = e.target.closest(".storage-row");
      if (!row || row !== activeStorageRow) return;
      const next = e.relatedTarget;
      if (next && row.contains(next)) return;
      hideStorageTooltip();
    }});

    function handleStacksClick(e) {{
      const fav = e.target.closest("[data-fav]");
      if (fav) {{
        toggleFavorite(fav.dataset.fav);
        return;
      }}
      const collapseStack = e.target.closest("[data-collapse-stack]");
      if (collapseStack) {{
        toggleStackCollapsed(collapseStack.dataset.collapseStack);
        return;
      }}
      const hideStack = e.target.closest("[data-hide-stack]");
      if (hideStack) {{
        toggleHiddenStack(hideStack.dataset.hideStack);
        return;
      }}
      const hide = e.target.closest("[data-hide]");
      if (hide) {{
        toggleHidden(hide.dataset.hide);
        return;
      }}
      const btn = e.target.closest("[data-logs]");
      if (!btn) return;
      openLogs(btn.dataset.logs, btn.dataset.name);
    }}

    document.getElementById("stacks").addEventListener("click", handleStacksClick);
    document.getElementById("hidden-stacks").addEventListener("click", handleStacksClick);

    document.getElementById("hidden-toggle").addEventListener("click", toggleShowHidden);

    document.getElementById("settings-open").addEventListener("click", openSettings);
    document.getElementById("settings-close").addEventListener("click", closeSettings);
    document.getElementById("settings-compact-view").addEventListener("change", (e) => {{
      setCompactView(e.target.checked);
    }});
    document.getElementById("settings-truncate-names").addEventListener("change", (e) => {{
      setTruncateNames(e.target.checked);
    }});
    document.getElementById("settings-modal").addEventListener("click", (e) => {{
      if (e.target.id === "settings-modal") closeSettings();
    }});

    document.getElementById("logs-close").addEventListener("click", closeLogs);
    document.getElementById("logs-copy").addEventListener("click", copyLogs);
    document.getElementById("logs-modal").addEventListener("click", (e) => {{
      if (e.target.id === "logs-modal") closeLogs();
    }});
    document.getElementById("logs-scroll").addEventListener("scroll", updateScrollProgress);
    document.addEventListener("keydown", (e) => {{
      if (e.key !== "Escape") return;
      if (!document.getElementById("settings-modal").hidden) closeSettings();
      else if (!document.getElementById("logs-modal").hidden) closeLogs();
    }});

    document.addEventListener("visibilitychange", () => {{
      if (!document.hidden) refresh();
    }});

    applySettings();
    setClock();
    renderMeters();
    recordUsageSamples();
    render();
    setInterval(refresh, REFRESH_MS);
  </script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        reload_if_stale()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        host = request_host(self)

        if path == "/api/status":
            body = json.dumps(page_payload(host), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/api/logs/"):
            ref = urllib.parse.unquote(path[len("/api/logs/") :])
            self._stream_logs(ref)
            return

        if path not in {"/", "/index.html"}:
            self.send_error(404, "Not Found")
            return

        body = render_page(host).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _stream_logs(self, ref: str) -> None:
        conn = None
        try:
            conn, resp, tty = open_container_logs(ref)
        except ValueError as exc:
            body = str(exc).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001
            body = str(exc).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in iter_docker_log_chunks(resp, tty):
                text = chunk.decode("utf-8", errors="replace").encode("utf-8")
                self.wfile.write(text)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            if conn is not None:
                conn.close()

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    try:
        httpd = ReusableTCPServer((HOST, PORT), Handler)
    except PermissionError:
        raise SystemExit(f"Permissão negada para a porta {PORT}.") from None
    with httpd:
        print(f"Homelab homepage em http://{HOST}:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nEncerrado.")


if __name__ == "__main__":
    main()
