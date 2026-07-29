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
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
HOST_ROOT = os.environ.get("HOST_ROOT", "/host").rstrip("/")
SAFE_CONTAINER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LOG_TAIL = 200
CACHE_FAST_INTERVAL = 5.0
CACHE_STORAGE_INTERVAL = 60.0
DB_PATH = os.environ.get("DB_PATH", "/app/data/homepage.db")
DEFAULT_SETTINGS = {"compactView": False, "truncateNames": False}
_db_lock = threading.Lock()
_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    position INTEGER NOT NULL,
    name TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS hidden_containers (
    name TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS hidden_stacks (
    name TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS collapsed_stacks (
    stack_key TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
"""


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


def docker_request(method: str, path: str, body: bytes | None = None) -> None:
    conn = UnixHTTPConnection(DOCKER_SOCKET)
    try:
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Docker API {resp.status}: {data.decode(errors='replace')}")
    finally:
        conn.close()


def validate_container_ref(ref: str) -> str:
    if not SAFE_CONTAINER_REF.match(ref):
        raise ValueError("Referência de container inválida")
    return urllib.parse.quote(ref, safe="")


def container_start(ref: str) -> None:
    docker_request("POST", f"/containers/{validate_container_ref(ref)}/start")


def container_stop(ref: str) -> None:
    docker_request("POST", f"/containers/{validate_container_ref(ref)}/stop")


def container_restart(ref: str) -> None:
    docker_request("POST", f"/containers/{validate_container_ref(ref)}/restart")


def container_remove(ref: str, *, force: bool = False) -> None:
    quoted = validate_container_ref(ref)
    suffix = "?force=true" if force else ""
    docker_request("DELETE", f"/containers/{quoted}{suffix}")


def validate_stack_name(name: str) -> str:
    if not name or "/" in name or "\x00" in name:
        raise ValueError("Nome de stack inválido")
    return name


def containers_in_stack(stack: str) -> list[dict]:
    validate_stack_name(stack)
    return [c for c in running_containers() if c["stack"] == stack]


def is_container_running(status: str) -> bool:
    return (status or "").lower().startswith("up")


def stack_stop(stack: str) -> None:
    for c in containers_in_stack(stack):
        if is_container_running(c["status"]):
            container_stop(c["id"])


def stack_restart(stack: str) -> None:
    for c in containers_in_stack(stack):
        container_restart(c["id"])


def stack_remove(stack: str) -> None:
    for c in containers_in_stack(stack):
        container_remove(c["id"], force=True)


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


PREFERRED_HWMON_NAMES = frozenset(
    {"coretemp", "k10temp", "zenpower", "applesmc", "cpu_thermal", "soc_thermal"}
)
SKIP_HWMON_NAMES = frozenset({"acpitz"})


def _sysfs_roots() -> list[str]:
    if _using_host_storage():
        return [HOST_ROOT, ""]
    return [""]


def _sysfs_path(root: str, *parts: str) -> str:
    path = os.path.join(root, *parts) if root else os.path.join("/", *parts)
    return path


def _read_temp_millidegrees(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            val = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if val <= 0 or val > 150_000:
        return None
    return val


def _hwmon_readings(root: str) -> list[tuple[str, int]]:
    hwmon_dir = _sysfs_path(root, "sys", "class", "hwmon")
    rows: list[tuple[str, int]] = []
    try:
        entries = sorted(os.listdir(hwmon_dir))
    except OSError:
        return rows
    for entry in entries:
        if not entry.startswith("hwmon"):
            continue
        base = os.path.join(hwmon_dir, entry)
        sensor_name = "sensor"
        try:
            with open(os.path.join(base, "name"), encoding="utf-8") as f:
                sensor_name = f.read().strip() or sensor_name
        except OSError:
            pass
        try:
            files = sorted(os.listdir(base))
        except OSError:
            continue
        for fname in files:
            if not fname.startswith("temp") or not fname.endswith("_input"):
                continue
            val = _read_temp_millidegrees(os.path.join(base, fname))
            if val is not None:
                rows.append((sensor_name, val))
    return rows


def _thermal_zone_readings(root: str) -> list[tuple[str, int]]:
    thermal_dir = _sysfs_path(root, "sys", "class", "thermal")
    rows: list[tuple[str, int]] = []
    try:
        entries = sorted(os.listdir(thermal_dir))
    except OSError:
        return rows
    for entry in entries:
        if not entry.startswith("thermal_zone"):
            continue
        base = os.path.join(thermal_dir, entry)
        zone_type = "thermal"
        try:
            with open(os.path.join(base, "type"), encoding="utf-8") as f:
                zone_type = f.read().strip() or zone_type
        except OSError:
            pass
        val = _read_temp_millidegrees(os.path.join(base, "temp"))
        if val is not None:
            rows.append((zone_type, val))
    return rows


def temperature_stats() -> tuple[float | None, str]:
    hwmon: list[tuple[str, int]] = []
    thermal: list[tuple[str, int]] = []
    for root in _sysfs_roots():
        hwmon.extend(_hwmon_readings(root))
        thermal.extend(_thermal_zone_readings(root))

    preferred_hwmon = [val for name, val in hwmon if name in PREFERRED_HWMON_NAMES]
    if preferred_hwmon:
        return max(preferred_hwmon) / 1000, "CPU"

    pkg_thermal = [
        val for name, val in thermal if "pkg" in name.lower() or "cpu" in name.lower()
    ]
    if pkg_thermal:
        return max(pkg_thermal) / 1000, "CPU"

    fallback_hwmon = [val for name, val in hwmon if name not in SKIP_HWMON_NAMES]
    if fallback_hwmon:
        return max(fallback_hwmon) / 1000, "CPU"

    if thermal:
        return max(val for _, val in thermal) / 1000, "Sistema"

    return None, ""


def temperature_color(celsius: float) -> str:
    if celsius >= 85:
        return "#f85149"
    if celsius >= 65:
        return "#e3b341"
    return "#3fb950"


def temperature_sensor_details() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()

    for root in _sysfs_roots():
        hwmon_dir = _sysfs_path(root, "sys", "class", "hwmon")
        try:
            entries = sorted(os.listdir(hwmon_dir))
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith("hwmon"):
                continue
            base = os.path.join(hwmon_dir, entry)
            chip = "sensor"
            try:
                with open(os.path.join(base, "name"), encoding="utf-8") as f:
                    chip = f.read().strip() or chip
            except OSError:
                pass
            try:
                files = sorted(os.listdir(base))
            except OSError:
                continue
            for fname in files:
                if not fname.startswith("temp") or not fname.endswith("_input"):
                    continue
                val = _read_temp_millidegrees(os.path.join(base, fname))
                if val is None:
                    continue
                label = chip
                label_path = os.path.join(base, fname.replace("_input", "_label"))
                try:
                    with open(label_path, encoding="utf-8") as f:
                        part = f.read().strip()
                        if part:
                            label = f"{chip} · {part}"
                except OSError:
                    pass
                key = f"{root}:{label}"
                if key in seen:
                    continue
                seen.add(key)
                celsius = val / 1000
                rows.append(
                    {
                        "name": label,
                        "value": celsius,
                        "display": f"{celsius:.0f}°C",
                    }
                )

        thermal_dir = _sysfs_path(root, "sys", "class", "thermal")
        try:
            zones = sorted(os.listdir(thermal_dir))
        except OSError:
            continue
        for entry in zones:
            if not entry.startswith("thermal_zone"):
                continue
            base = os.path.join(thermal_dir, entry)
            zone_type = "thermal"
            try:
                with open(os.path.join(base, "type"), encoding="utf-8") as f:
                    zone_type = f.read().strip() or zone_type
            except OSError:
                pass
            val = _read_temp_millidegrees(os.path.join(base, "temp"))
            if val is None:
                continue
            label = f"{entry} · {zone_type}"
            key = f"{root}:{label}"
            if key in seen:
                continue
            seen.add(key)
            celsius = val / 1000
            rows.append(
                {
                    "name": label,
                    "value": celsius,
                    "display": f"{celsius:.0f}°C",
                }
            )

    rows.sort(key=lambda row: row["value"], reverse=True)
    return rows


def _format_bytes(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _format_pct(value: float) -> str:
    if value <= 0:
        return "0%"
    for decimals in (1, 2, 3, 4):
        if round(value, decimals) > 0:
            return f"{value:.{decimals}f}%"
    return f"{value:.4f}%"


def _container_name(container: dict) -> str:
    names = [n.lstrip("/") for n in (container.get("Names") or [])]
    cid = container.get("Id") or ""
    return names[0] if names else cid[:12]


def _container_stack(container: dict) -> str:
    labels = container.get("Labels") or {}
    return labels.get("com.docker.compose.project") or "sem stack"


def _container_cpu_pct(stats: dict) -> float:
    cpu = stats.get("cpu_stats") or {}
    precpu = stats.get("precpu_stats") or {}
    cpu_usage = (cpu.get("cpu_usage") or {}).get("total_usage")
    pre_cpu_usage = (precpu.get("cpu_usage") or {}).get("total_usage")
    system_usage = cpu.get("system_cpu_usage")
    pre_system_usage = precpu.get("system_cpu_usage")
    if None in (cpu_usage, pre_cpu_usage, system_usage, pre_system_usage):
        return 0.0
    cpu_delta = cpu_usage - pre_cpu_usage
    system_delta = system_usage - pre_system_usage
    if system_delta <= 0 or cpu_delta < 0:
        return 0.0
    online = cpu.get("online_cpus") or 1
    return (cpu_delta / system_delta) * online * 100.0


def _container_memory_bytes(stats: dict) -> int:
    usage = (stats.get("memory_stats") or {}).get("usage")
    return int(usage) if isinstance(usage, int) and usage > 0 else 0


def _fetch_container_stats(container: dict) -> tuple[dict, dict] | None:
    cid = container.get("Id") or ""
    if not cid:
        return None
    quoted = urllib.parse.quote(cid, safe="")
    try:
        stats = docker_get(f"/containers/{quoted}/stats?stream=false")
    except RuntimeError:
        return None
    return container, stats


def _running_container_stats() -> list[tuple[dict, dict]]:
    containers = docker_get("/containers/json")
    rows: list[tuple[dict, dict]] = []
    workers = min(8, max(len(containers), 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_container_stats, container) for container in containers]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                rows.append(result)
    return rows


def _meter_detail_cpu_from_stats(pairs: list[tuple[dict, dict]]) -> dict:
    rows: list[dict] = []
    for container, stats in pairs:
        pct = _container_cpu_pct(stats)
        rows.append(
            {
                "name": _container_name(container),
                "sub": _container_stack(container),
                "value": pct,
                "display": _format_pct(pct),
            }
        )
    rows.sort(key=lambda row: row["value"], reverse=True)
    return {"rows": rows, "total": 100}


def _meter_detail_ram_from_stats(pairs: list[tuple[dict, dict]]) -> dict:
    ram_pct, ram_used_gb, ram_total_gb = ram_stats()
    ram_total_bytes = int(ram_total_gb * (1024**3))
    rows: list[dict] = []
    for container, stats in pairs:
        usage = _container_memory_bytes(stats)
        pct_of_total = (usage / ram_total_bytes * 100) if ram_total_bytes > 0 else 0.0
        rows.append(
            {
                "name": _container_name(container),
                "sub": _container_stack(container),
                "value": usage,
                "display": f"{_format_bytes(usage)} · {_format_pct(pct_of_total)}",
            }
        )
    rows.sort(key=lambda row: row["value"], reverse=True)
    return {
        "rows": rows,
        "total": max(ram_total_bytes, 1),
        "summary": {
            "sub": f"{ram_used_gb:.1f} / {ram_total_gb:.0f} GB",
            "display": f"{ram_pct}%",
        },
    }


def meter_detail_cpu() -> dict:
    return _meter_detail_cpu_from_stats(_running_container_stats())


def meter_detail_ram() -> dict:
    return _meter_detail_ram_from_stats(_running_container_stats())


def _image_display_name(image: dict) -> str:
    tags = image.get("RepoTags") or []
    if tags:
        return tags[0]
    image_id = image.get("Id") or ""
    if image_id.startswith("sha256:"):
        return image_id[7:19]
    return image_id or "sem tag"


def meter_detail_storage() -> dict:
    df = docker_get("/system/df")
    volume_sizes = {
        vol.get("Name", ""): int((vol.get("UsageData") or {}).get("Size") or 0)
        for vol in df.get("Volumes") or []
    }

    container_rows: list[dict] = []
    for container in df.get("Containers") or []:
        writable = int(container.get("SizeRw") or 0)
        image_layer = int(container.get("SizeRootFs") or 0)
        volumes = 0
        for mount in container.get("Mounts") or []:
            if (mount.get("Type") or "").lower() != "volume":
                continue
            volumes += volume_sizes.get(mount.get("Name") or "", 0)
        total = writable + image_layer + volumes
        container_rows.append(
            {
                "name": _container_name(container),
                "sub": _container_stack(container),
                "value": total,
                "display": _format_bytes(total),
                "writable": _format_bytes(writable),
                "image": _format_bytes(image_layer),
                "volumes": _format_bytes(volumes),
            }
        )
    container_rows.sort(key=lambda row: row["value"], reverse=True)

    image_rows: list[dict] = []
    for image in df.get("Images") or []:
        size = int(image.get("Size") or 0)
        image_rows.append(
            {
                "name": _image_display_name(image),
                "sub": f"{image.get('Containers', 0)} container(s)",
                "value": size,
                "display": _format_bytes(size),
            }
        )
    image_rows.sort(key=lambda row: row["value"], reverse=True)

    volume_rows: list[dict] = []
    for volume in df.get("Volumes") or []:
        size = int((volume.get("UsageData") or {}).get("Size") or 0)
        ref_count = int((volume.get("UsageData") or {}).get("RefCount") or 0)
        volume_rows.append(
            {
                "name": volume.get("Name") or "volume",
                "sub": f"{ref_count} referência(s)",
                "value": size,
                "display": _format_bytes(size),
            }
        )
    volume_rows.sort(key=lambda row: row["value"], reverse=True)

    build_rows: list[dict] = []
    for entry in df.get("BuildCache") or []:
        size = int(entry.get("Size") or 0)
        description = entry.get("Description") or entry.get("ID") or "build cache"
        shared = "compartilhado" if entry.get("Shared") else "exclusivo"
        build_rows.append(
            {
                "name": description,
                "sub": shared,
                "value": size,
                "display": _format_bytes(size),
            }
        )
    build_rows.sort(key=lambda row: row["value"], reverse=True)

    main_disk, extra_mounts = storage_mounts()
    _, _, storage_total_gb = _storage_aggregate(main_disk, extra_mounts)
    storage_total_bytes = max(int(storage_total_gb * (1024**3)), 1)

    return {
        "total": storage_total_bytes,
        "sections": [
            {"title": "Containers", "rows": container_rows, "columns": ["writable", "image", "volumes"]},
            {"title": "Imagens", "rows": image_rows},
            {"title": "Volumes", "rows": volume_rows},
            {"title": "Build cache", "rows": build_rows},
        ],
    }


def meter_detail_temp() -> dict:
    return {"rows": temperature_sensor_details(), "total": 100}


METER_DETAIL_HANDLERS = {
    "cpu": meter_detail_cpu,
    "ram": meter_detail_ram,
    "storage": meter_detail_storage,
    "temp": meter_detail_temp,
}


class MetricsCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_storage_refresh = 0.0
        self._containers: list[dict] = []
        self._meters: list[dict] = []
        self._status_error: str | None = None
        self._meter_details: dict[str, dict] = {}

    def warm(self) -> None:
        self._refresh_fast()
        self._refresh_storage()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="metrics-cache", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(CACHE_FAST_INTERVAL):
            self._refresh_fast()
            if time.monotonic() - self._last_storage_refresh >= CACHE_STORAGE_INTERVAL:
                self._refresh_storage()

    def _refresh_fast(self) -> None:
        containers: list[dict] | None = None
        meters: list[dict] | None = None
        status_error: str | None = None
        meter_updates: dict[str, dict] = {}

        try:
            containers = running_containers()
        except Exception as exc:  # noqa: BLE001
            status_error = str(exc)

        try:
            meters = build_meters()
        except Exception as exc:  # noqa: BLE001
            if status_error is None:
                status_error = str(exc)

        try:
            meter_updates["temp"] = meter_detail_temp()
        except Exception as exc:  # noqa: BLE001
            meter_updates["temp"] = {"error": str(exc)}

        try:
            stats_pairs = _running_container_stats()
            meter_updates["cpu"] = _meter_detail_cpu_from_stats(stats_pairs)
            meter_updates["ram"] = _meter_detail_ram_from_stats(stats_pairs)
        except Exception as exc:  # noqa: BLE001
            meter_updates["cpu"] = {"error": str(exc)}
            meter_updates["ram"] = {"error": str(exc)}

        with self._lock:
            if containers is not None:
                self._containers = containers
            if meters is not None:
                self._meters = meters
            if status_error is not None:
                self._status_error = status_error
            elif containers is not None:
                self._status_error = None
            for kind, data in meter_updates.items():
                if "error" not in data or kind not in self._meter_details:
                    self._meter_details[kind] = data

    def _refresh_storage(self) -> None:
        try:
            data = meter_detail_storage()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if "storage" not in self._meter_details:
                    self._meter_details["storage"] = {"error": str(exc)}
            return

        with self._lock:
            self._meter_details["storage"] = data
            self._last_storage_refresh = time.monotonic()

    def page_payload(self, req_host: str) -> dict:
        with self._lock:
            return {
                "host": req_host,
                "build": app_build(),
                "loaded_build": _MODULE_BUILD,
                "error": self._status_error,
                "containers": deepcopy(self._containers),
                "meters": deepcopy(self._meters),
            }

    def meter_detail(self, kind: str) -> dict:
        with self._lock:
            data = self._meter_details.get(kind)
            if data is None:
                return {"kind": kind, "error": "Dados ainda não disponíveis"}
            payload = deepcopy(data)
            payload["kind"] = kind
            return payload


_METRICS_CACHE: MetricsCache | None = None


def get_metrics_cache() -> MetricsCache:
    global _METRICS_CACHE
    if _METRICS_CACHE is None:
        _METRICS_CACHE = MetricsCache()
    return _METRICS_CACHE


def meter_detail_payload(kind: str) -> dict:
    if kind not in METER_DETAIL_HANDLERS:
        raise ValueError("Tipo de métrica inválido")
    return get_metrics_cache().meter_detail(kind)


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


def build_meters() -> list[dict]:
    cpu, ncpu = cpu_percent()
    ram_pct, ram_used, ram_total = ram_stats()
    temp_c, temp_label = temperature_stats()
    main_disk, extra_mounts = storage_mounts()
    storage_pct, storage_used, storage_total = _storage_aggregate(main_disk, extra_mounts)

    if temp_c is None:
        temp_meter = {
            "label": "Temperatura",
            "display": "—",
            "sub": "indisponível",
            "showSub": True,
            "color": "#8b94a3",
            "barWidth": "0%",
            "showBar": False,
            "showCaption": False,
            "caption": "",
            "detailKey": "temp",
        }
    else:
        temp_pct = round(min(100.0, max(0.0, temp_c)))
        temp_color = temperature_color(temp_c)
        temp_meter = {
            "label": "Temperatura",
            "display": f"{temp_c:.0f}°C",
            "sub": temp_label,
            "showSub": True,
            "color": temp_color,
            "barWidth": f"{temp_pct}%",
            "showBar": True,
            "showChart": True,
            "chartKey": "temp",
            "pct": temp_pct,
            "showCaption": False,
            "caption": "",
            "detailKey": "temp",
        }

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
            "detailKey": "cpu",
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
            "detailKey": "ram",
        },
        temp_meter,
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
            "detailKey": "storage",
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


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.executescript(_DB_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _read_prefs(conn: sqlite3.Connection) -> dict:
    favorites = [
        row["name"]
        for row in conn.execute("SELECT name FROM favorites ORDER BY position ASC")
    ]
    hidden_containers = [
        row["name"] for row in conn.execute("SELECT name FROM hidden_containers ORDER BY name")
    ]
    hidden_stacks = [
        row["name"] for row in conn.execute("SELECT name FROM hidden_stacks ORDER BY name")
    ]
    collapsed_stacks = [
        row["stack_key"]
        for row in conn.execute("SELECT stack_key FROM collapsed_stacks ORDER BY stack_key")
    ]
    settings = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in settings:
            settings[row["key"]] = row["value"] == "true"
    return {
        "favorites": favorites,
        "hiddenContainers": hidden_containers,
        "hiddenStacks": hidden_stacks,
        "collapsedStacks": collapsed_stacks,
        "settings": settings,
    }


def get_prefs() -> dict:
    with _db_lock:
        conn = _db_connect()
        try:
            return _read_prefs(conn)
        finally:
            conn.close()


def update_prefs(data: dict) -> dict:
    with _db_lock:
        conn = _db_connect()
        try:
            if "favorites" in data:
                names = [str(name) for name in data["favorites"]]
                conn.execute("DELETE FROM favorites")
                conn.executemany(
                    "INSERT INTO favorites (position, name) VALUES (?, ?)",
                    list(enumerate(names)),
                )
            if "hiddenContainers" in data:
                names = [str(name) for name in data["hiddenContainers"]]
                conn.execute("DELETE FROM hidden_containers")
                conn.executemany(
                    "INSERT INTO hidden_containers (name) VALUES (?)",
                    [(name,) for name in names],
                )
            if "hiddenStacks" in data:
                names = [str(name) for name in data["hiddenStacks"]]
                conn.execute("DELETE FROM hidden_stacks")
                conn.executemany(
                    "INSERT INTO hidden_stacks (name) VALUES (?)",
                    [(name,) for name in names],
                )
            if "collapsedStacks" in data:
                keys = [str(key) for key in data["collapsedStacks"]]
                conn.execute("DELETE FROM collapsed_stacks")
                conn.executemany(
                    "INSERT INTO collapsed_stacks (stack_key) VALUES (?)",
                    [(key,) for key in keys],
                )
            if "settings" in data:
                settings = data["settings"]
                if isinstance(settings, dict):
                    for key in DEFAULT_SETTINGS:
                        if key in settings:
                            conn.execute(
                                "INSERT INTO settings (key, value) VALUES (?, ?) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                (key, "true" if settings[key] else "false"),
                            )
            conn.commit()
            return _read_prefs(conn)
        finally:
            conn.close()


def page_payload(req_host: str) -> dict:
    return get_metrics_cache().page_payload(req_host)


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
    .header-settings-btn svg[hidden] {{
      display: none;
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
      position: relative;
      background: #141922;
      border: 1px solid #1e2530;
      border-radius: 12px;
      padding: 16px 18px;
      min-width: 0;
      overflow: hidden;
    }}
    .meter-expand-btn {{
      position: absolute;
      top: 10px;
      right: 10px;
      z-index: 2;
      display: none;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      padding: 0;
      color: #5b6472;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 7px;
      cursor: pointer;
      transition: color .12s, background .12s, border-color .12s;
    }}
    .meter:hover .meter-expand-btn,
    .meter:focus-within .meter-expand-btn {{
      display: inline-flex;
    }}
    .meter-expand-btn:hover {{
      color: #c9d1de;
      background: #1a2230;
      border-color: #2a3544;
    }}
    .meter-expand-btn svg {{
      width: 14px;
      height: 14px;
      display: block;
    }}
    .meter-top {{
      position: relative;
      display: flex;
      align-items: baseline;
      justify-content: flex-start;
      min-height: 18px;
    }}
    .meter-label {{
      font-size: 12px;
      color: #7d8695;
      font-weight: 600;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    .meter-sub {{
      position: absolute;
      top: 0;
      right: 10px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: #5b6472;
      white-space: nowrap;
      transition: right .15s ease;
    }}
    .meter:hover .meter-sub,
    .meter:focus-within .meter-sub {{
      right: 42px;
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
    .stack-collapse-btn,
    .stack-action-btn {{
      width: 28px;
      height: 28px;
      flex-shrink: 0;
    }}
    .stack-hide-btn svg,
    .stack-collapse-btn svg,
    .stack-action-btn svg {{
      width: 14px;
      height: 14px;
    }}
    .stack-actions {{
      display: inline-flex;
      align-items: center;
      gap: 1px;
    }}
    .stack-action-btn {{
      opacity: 0.45;
    }}
    .stack-head:hover .stack-action-btn,
    .stack-action-btn:focus-visible {{
      opacity: 1;
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
      grid-template-columns: minmax(0, 1.5fr) minmax(0, 0.9fr) 110px 150px minmax(0, 0.7fr) 72px;
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
      min-width: 0;
    }}
    body.truncate-names .name-text {{
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .container-actions {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 1px;
      flex-shrink: 0;
    }}
    .name-action-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      padding: 0;
      color: #6b7280;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 6px;
      cursor: pointer;
      opacity: 0.45;
      transition: opacity .12s, color .12s, background .12s, border-color .12s;
    }}
    .row:hover .name-action-btn,
    .name-action-btn:focus-visible {{
      opacity: 1;
    }}
    .name-action-btn:hover {{
      color: #e6e9ef;
      background: #1a2230;
      border-color: #2a3544;
    }}
    .name-action-btn.delete-btn:hover {{
      color: #f85149;
      background: rgba(248,81,73,.12);
      border-color: rgba(248,81,73,.25);
    }}
    .name-action-btn svg {{
      width: 14px;
      height: 14px;
      display: block;
    }}
    .image-text {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 12.5px;
      color: #8b94a3;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-align: start;
    }}
    .status-cell {{
      display: flex;
      align-items: center;
      justify-content: center;
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
    #meter-modal {{
      gap: 14px;
    }}
    .modal-nav-btn {{
      flex-shrink: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 42px;
      height: 42px;
      padding: 0;
      border: 1px solid #2a3544;
      border-radius: 50%;
      background: rgba(15, 19, 26, .92);
      color: #c9d1de;
      cursor: pointer;
      transition: color .12s, background .12s, border-color .12s;
    }}
    .modal-nav-btn:hover {{ background: #1a2230; color: #fff; }}
    .modal-nav-btn svg {{
      width: 20px;
      height: 20px;
    }}
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
    .modal.modal-md {{
      width: min(760px, 100%);
      height: auto;
      max-height: min(78vh, 720px);
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
    .meter-detail-loading,
    .meter-detail-empty,
    .meter-detail-error {{
      padding: 28px 18px;
      text-align: center;
      font-size: 14px;
      color: #7d8695;
    }}
    .meter-detail-error {{
      color: #f85149;
    }}
    .meter-detail-summary {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid #1a2030;
    }}
    .meter-detail-summary-sub {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      color: #c9d1de;
    }}
    .meter-detail-summary-pct {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 18px;
      font-weight: 700;
      color: #e6e9ef;
      white-space: nowrap;
    }}
    .meter-detail-section {{
      padding: 0 18px 18px;
    }}
    .meter-detail-section:first-child {{
      padding-top: 18px;
    }}
    .meter-detail-section-title {{
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: #7d8695;
      margin-bottom: 10px;
    }}
    .meter-detail-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 100px 72px;
      gap: 8px 16px;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid #1a2030;
    }}
    .meter-detail-row:last-child {{
      border-bottom: none;
    }}
    .meter-detail-info {{
      min-width: 0;
    }}
    .meter-detail-name {{
      font-size: 13px;
      font-weight: 600;
      color: #e6e9ef;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .meter-detail-sub,
    .meter-detail-meta {{
      margin-top: 2px;
      font-size: 11px;
      color: #7d8695;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .meter-detail-meta {{
      font-family: 'JetBrains Mono', monospace;
      color: #5b6472;
    }}
    .meter-detail-bar {{
      height: 6px;
      border-radius: 20px;
      background: #1e2530;
      overflow: hidden;
      min-width: 0;
    }}
    .meter-detail-bar > span {{
      display: block;
      height: 100%;
      border-radius: 20px;
    }}
    .meter-detail-value {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      font-weight: 600;
      color: #c9d1de;
      text-align: right;
      white-space: nowrap;
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
      grid-template-columns: minmax(0, 1.4fr) minmax(0, 0.85fr) 110px 150px minmax(0, 0.65fr) 60px;
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
    body.compact-view .name-action-btn {{
      width: 22px;
      height: 22px;
      border-radius: 5px;
    }}
    body.compact-view .name-action-btn svg {{
      width: 12px;
      height: 12px;
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
        grid-template-columns: minmax(0, 1fr) auto auto;
        gap: 10px;
      }}
      .row-name {{ grid-column: 1; }}
      .container-actions {{ grid-column: 2; }}
      .row-actions {{ grid-column: 3; grid-row: 1; }}
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
      #meter-modal {{ gap: 8px; padding: 16px 8px; }}
      .modal-nav-btn {{ width: 36px; height: 36px; }}
      .modal-nav-btn svg {{ width: 18px; height: 18px; }}
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
          <button type="button" class="header-settings-btn" id="fullscreen-toggle" title="Tela cheia" aria-label="Tela cheia">
            <svg class="fullscreen-enter-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
            <svg class="fullscreen-exit-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" hidden><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
          </button>
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

  <div id="meter-modal" class="modal-backdrop" hidden>
    <button type="button" class="modal-nav-btn" id="meter-prev" title="Anterior" aria-label="Métrica anterior">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>
    </button>
    <div class="modal modal-md" role="dialog" aria-modal="true" aria-labelledby="meter-modal-title">
      <div class="modal-head">
        <div class="modal-title-wrap">
          <span class="modal-label">Detalhes</span>
          <div class="modal-title" id="meter-modal-title"></div>
        </div>
        <button type="button" class="modal-close" id="meter-close" aria-label="Fechar">×</button>
      </div>
      <div class="modal-body" id="meter-modal-body"></div>
    </div>
    <button type="button" class="modal-nav-btn" id="meter-next" title="Próximo" aria-label="Próxima métrica">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
    </button>
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
          <button type="button" class="modal-icon-btn" id="logs-clear" title="Limpar logs" aria-label="Limpar logs">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6M14 11v6"/></svg>
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
    const EXPAND_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="m21 3-7 7"/><path d="M9 21H3v-6"/><path d="m3 21 7-7"/></svg>`;
    const PLAY_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>`;
    const STOP_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>`;
    const RESTART_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>`;
    const TRASH_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;

    let favorites = new Set();
    let hiddenContainers = new Set();
    let hiddenStacks = new Set();
    let collapsedStacks = new Set();
    let settings = {{ compactView: false, truncateNames: false }};
    let logsAbort = null;
    let logsStickBottom = true;
    let copyResetTimer = null;

    const LEGACY_KEYS = {{
      favorites: "homelab-homepage-favorites",
      hiddenContainers: "homelab-homepage-hidden",
      hiddenStacks: "homelab-homepage-hidden-stacks",
      collapsedStacks: "homelab-homepage-collapsed-stacks",
      settings: "homelab-homepage-settings",
    }};

    function readLegacyList(key) {{
      try {{
        const raw = localStorage.getItem(key);
        const list = raw ? JSON.parse(raw) : [];
        return Array.isArray(list) ? list.map(String) : [];
      }} catch {{
        return [];
      }}
    }}

    function readLegacyPrefs() {{
      let settingsData = {{}};
      try {{
        const raw = localStorage.getItem(LEGACY_KEYS.settings);
        settingsData = raw ? JSON.parse(raw) : {{}};
      }} catch {{
        settingsData = {{}};
      }}
      return {{
        favorites: readLegacyList(LEGACY_KEYS.favorites),
        hiddenContainers: readLegacyList(LEGACY_KEYS.hiddenContainers),
        hiddenStacks: readLegacyList(LEGACY_KEYS.hiddenStacks),
        collapsedStacks: readLegacyList(LEGACY_KEYS.collapsedStacks),
        settings: {{
          compactView: Boolean(settingsData.compactView),
          truncateNames: Boolean(settingsData.truncateNames),
        }},
      }};
    }}

    function hasLegacyPrefs() {{
      return Object.values(LEGACY_KEYS).some((key) => localStorage.getItem(key) !== null);
    }}

    function clearLegacyPrefs() {{
      Object.values(LEGACY_KEYS).forEach((key) => localStorage.removeItem(key));
    }}

    function hasAnyPrefs(prefs) {{
      return !!(
        prefs.favorites?.length ||
        prefs.hiddenContainers?.length ||
        prefs.hiddenStacks?.length ||
        prefs.collapsedStacks?.length ||
        prefs.settings?.compactView ||
        prefs.settings?.truncateNames
      );
    }}

    function applyPrefs(prefs) {{
      favorites = new Set(prefs.favorites || []);
      hiddenContainers = new Set(prefs.hiddenContainers || []);
      hiddenStacks = new Set(prefs.hiddenStacks || []);
      collapsedStacks = new Set(prefs.collapsedStacks || []);
      settings = {{
        compactView: Boolean(prefs.settings?.compactView),
        truncateNames: Boolean(prefs.settings?.truncateNames),
      }};
    }}

    async function loadPrefs() {{
      const legacy = hasLegacyPrefs() ? readLegacyPrefs() : null;

      const res = await fetch("/api/prefs", {{ cache: "no-store" }});
      if (!res.ok) throw new Error("HTTP " + res.status);
      let prefs = await res.json();

      if (legacy) {{
        if (!hasAnyPrefs(prefs) && hasAnyPrefs(legacy)) {{
          const putRes = await fetch("/api/prefs", {{
            method: "PUT",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(legacy),
          }});
          if (!putRes.ok) throw new Error("HTTP " + putRes.status);
          prefs = await putRes.json();
        }}
        clearLegacyPrefs();
      }}

      applyPrefs(prefs);
    }}

    async function savePrefs(partial) {{
      try {{
        const res = await fetch("/api/prefs", {{
          method: "PUT",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(partial),
        }});
        if (!res.ok) throw new Error("HTTP " + res.status);
      }} catch (e) {{
        const err = document.getElementById("error");
        err.hidden = false;
        err.textContent = "Falha ao salvar preferências: " + (e && e.message ? e.message : e);
      }}
    }}

    function saveFavorites() {{
      savePrefs({{ favorites: [...favorites] }});
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

    function saveHidden() {{
      savePrefs({{
        hiddenContainers: [...hiddenContainers],
        hiddenStacks: [...hiddenStacks],
      }});
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

    function saveCollapsedStacks() {{
      savePrefs({{ collapsedStacks: [...collapsedStacks] }});
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

    function saveSettings() {{
      savePrefs({{ settings }});
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
      if (document.getElementById("logs-modal").hidden && document.getElementById("meter-modal").hidden) {{
        document.body.style.overflow = "";
      }}
    }}

    function isFullscreen() {{
      return Boolean(document.fullscreenElement);
    }}

    function toggleFullscreen() {{
      if (isFullscreen()) {{
        document.exitFullscreen();
      }} else {{
        document.documentElement.requestFullscreen();
      }}
    }}

    function updateFullscreenButton() {{
      const btn = document.getElementById("fullscreen-toggle");
      if (!btn) return;
      const fullscreen = isFullscreen();
      btn.title = fullscreen ? "Sair da tela cheia" : "Tela cheia";
      btn.setAttribute("aria-label", fullscreen ? "Sair da tela cheia" : "Tela cheia");
      const enterIcon = btn.querySelector(".fullscreen-enter-icon");
      const exitIcon = btn.querySelector(".fullscreen-exit-icon");
      if (enterIcon) enterIcon.hidden = fullscreen;
      if (exitIcon) exitIcon.hidden = !fullscreen;
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
      const favByName = new Map();
      const restRows = [];
      rows.forEach((c) => {{
        if (withFavorites && isFavorite(c.name)) favByName.set(c.name, c);
        else restRows.push(c);
      }});
      const favRows = withFavorites
        ? [...favorites].map((name) => favByName.get(name)).filter(Boolean)
        : [];

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

    function renderMeterExpandBtn(m) {{
      if (!m.detailKey) return "";
      return `<button type="button" class="meter-expand-btn" data-meter-detail="${{esc(m.detailKey)}}" data-meter-label="${{esc(m.label)}}" title="Expandir ${{esc(m.label)}}" aria-label="Expandir detalhes de ${{esc(m.label)}}">${{EXPAND_ICON}}</button>`;
    }}

    function meterBarColor(kind, value) {{
      if (kind === "temp") {{
        if (value >= 85) return "#f85149";
        if (value >= 65) return "#e3b341";
        return "#3fb950";
      }}
      const pct = kind === "cpu" ? value : Math.min(100, value);
      if (pct >= 85) return "#f85149";
      if (pct >= 65) return "#e3b341";
      return "#3fb950";
    }}

    function renderDetailRow(row, total, kind) {{
      const pct = total > 0 ? Math.min(100, Math.round((row.value / total) * 100)) : 0;
      const color = meterBarColor(kind, kind === "cpu" || kind === "temp" ? row.value : pct);
      const meta = row.writable
        ? `<div class="meter-detail-meta">RW ${{esc(row.writable)}} · imagem ${{esc(row.image)}} · volumes ${{esc(row.volumes)}}</div>`
        : "";
      return `
        <div class="meter-detail-row">
          <div class="meter-detail-info">
            <div class="meter-detail-name" title="${{esc(row.name)}}">${{esc(row.name)}}</div>
            ${{row.sub ? `<div class="meter-detail-sub">${{esc(row.sub)}}</div>` : ""}}
            ${{meta}}
          </div>
          <div class="meter-detail-bar"><span style="width:${{pct}}%;background:${{esc(color)}}"></span></div>
          <div class="meter-detail-value">${{esc(row.display)}}</div>
        </div>
      `;
    }}

    function renderDetailSection(section, kind, total) {{
      const rows = section.rows || [];
      if (!rows.length) {{
        return `
          <div class="meter-detail-section">
            <div class="meter-detail-section-title">${{esc(section.title)}}</div>
            <div class="meter-detail-empty">Nenhum item</div>
          </div>
        `;
      }}
      return `
        <div class="meter-detail-section">
          <div class="meter-detail-section-title">${{esc(section.title)}}</div>
          ${{rows.map((row) => renderDetailRow(row, total, kind)).join("")}}
        </div>
      `;
    }}

    function renderMeterDetailSummary(summary) {{
      if (!summary) return "";
      return `
        <div class="meter-detail-summary">
          <div class="meter-detail-summary-sub">${{esc(summary.sub || "")}}</div>
          <div class="meter-detail-summary-pct">${{esc(summary.display || "")}}</div>
        </div>
      `;
    }}

    function renderMeterDetail(data) {{
      if (data.error) {{
        return `<div class="meter-detail-error">${{esc(data.error)}}</div>`;
      }}
      const total = data.total || 1;
      const summary = renderMeterDetailSummary(data.summary);
      if (data.sections) {{
        return summary + data.sections.map((section) => renderDetailSection(section, data.kind, total)).join("");
      }}
      const rows = data.rows || [];
      if (!rows.length) {{
        return summary + `<div class="meter-detail-empty">Nenhum dado disponível</div>`;
      }}
      return summary + `<div class="meter-detail-section">${{rows.map((row) => renderDetailRow(row, total, data.kind)).join("")}}</div>`;
    }}

    let meterDetailAbort = null;
    let meterDetailTimer = null;
    let meterDetailKind = null;

    function stopMeterDetailRefresh() {{
      if (meterDetailTimer) {{
        clearInterval(meterDetailTimer);
        meterDetailTimer = null;
      }}
    }}

    function startMeterDetailRefresh(kind) {{
      stopMeterDetailRefresh();
      if (kind !== "cpu") return;
      meterDetailTimer = setInterval(() => {{
        if (document.hidden || meterDetailKind !== kind) return;
        loadMeterDetail(kind);
      }}, REFRESH_MS);
    }}

    async function loadMeterDetail(kind, {{ initial = false }} = {{}}) {{
      const modal = document.getElementById("meter-modal");
      const body = document.getElementById("meter-modal-body");
      if (modal.hidden || meterDetailKind !== kind) return;

      if (meterDetailAbort) {{
        meterDetailAbort.abort();
      }}
      const ctrl = new AbortController();
      meterDetailAbort = ctrl;

      try {{
        const res = await fetch("/api/meters/" + encodeURIComponent(kind), {{
          cache: "no-store",
          signal: ctrl.signal,
        }});
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        if (ctrl.signal.aborted || modal.hidden || meterDetailKind !== kind) return;
        body.innerHTML = renderMeterDetail(data);
      }} catch (e) {{
        if (e && e.name === "AbortError") return;
        if (initial) {{
          body.innerHTML = `<div class="meter-detail-error">Falha ao carregar: ${{esc(e && e.message ? e.message : e)}}</div>`;
        }}
      }} finally {{
        if (meterDetailAbort === ctrl) meterDetailAbort = null;
      }}
    }}

    function closeMeterDetail() {{
      meterDetailKind = null;
      stopMeterDetailRefresh();
      if (meterDetailAbort) {{
        meterDetailAbort.abort();
        meterDetailAbort = null;
      }}
      document.getElementById("meter-modal").hidden = true;
      if (document.getElementById("settings-modal").hidden && document.getElementById("logs-modal").hidden) {{
        document.body.style.overflow = "";
      }}
    }}

    function getMeterDetailNav() {{
      return DATA.meters
        .filter((m) => m.detailKey)
        .map((m) => ({{ kind: m.detailKey, label: m.label }}));
    }}

    function navigateMeterDetail(delta) {{
      if (!meterDetailKind) return;
      const nav = getMeterDetailNav();
      const idx = nav.findIndex((item) => item.kind === meterDetailKind);
      if (idx < 0) return;
      const next = nav[(idx + delta + nav.length) % nav.length];
      openMeterDetail(next.kind, next.label);
    }}

    async function openMeterDetail(kind, label) {{
      if (meterDetailAbort) {{
        meterDetailAbort.abort();
        meterDetailAbort = null;
      }}
      stopMeterDetailRefresh();
      meterDetailKind = kind;

      const modal = document.getElementById("meter-modal");
      const body = document.getElementById("meter-modal-body");
      document.getElementById("meter-modal-title").textContent = label;
      body.innerHTML = `<div class="meter-detail-loading">Carregando…</div>`;
      modal.hidden = false;
      document.body.style.overflow = "hidden";

      await loadMeterDetail(kind, {{ initial: true }});
      if (meterDetailKind === kind) startMeterDetailRefresh(kind);
    }}

    function renderMeter(m) {{
      if (m.type === "storage") {{
        const bars = [renderStorageBar(m.main)]
          .concat((m.mounts || []).map(renderStorageBar))
          .join("");
        return `
          <div class="meter storage">
            ${{renderMeterExpandBtn(m)}}
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
          ${{renderMeterExpandBtn(m)}}
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
    const usageHistory = {{ cpu: [], ram: [], temp: [] }};

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

    function isContainerRunning(status) {{
      return (status || "").toLowerCase().startsWith("up");
    }}

    function stackHasRunning(stack) {{
      return stack.containers.some((c) => isContainerRunning(c.status));
    }}

    function renderStackActions(stack) {{
      if (stack.isFavorites || !stack.showTitle) return "";
      const running = stackHasRunning(stack);
      const lifecycleBtn = running
        ? `<button type="button" class="name-action-btn stack-action-btn" data-stack-action="stop" data-stack-name="${{esc(stack.name)}}" title="Parar stack" aria-label="Parar stack ${{esc(stack.name)}}">${{STOP_ICON}}</button>`
        : "";
      return `
        <div class="stack-actions">
          ${{lifecycleBtn}}
          <button type="button" class="name-action-btn stack-action-btn" data-stack-action="restart" data-stack-name="${{esc(stack.name)}}" title="Reiniciar stack" aria-label="Reiniciar stack ${{esc(stack.name)}}">${{RESTART_ICON}}</button>
          <button type="button" class="name-action-btn stack-action-btn delete-btn" data-stack-action="delete" data-stack-name="${{esc(stack.name)}}" title="Apagar stack" aria-label="Apagar stack ${{esc(stack.name)}}">${{TRASH_ICON}}</button>
        </div>
      `;
    }}

    function renderContainerRow(c) {{
      const hidden = isHidden(c.name);
      const running = isContainerRunning(c.status);
      const lifecycleBtn = running
        ? `<button type="button" class="name-action-btn" data-action="stop" data-id="${{esc(c.id)}}" data-name="${{esc(c.name)}}" title="Parar" aria-label="Parar ${{esc(c.name)}}">${{STOP_ICON}}</button>`
        : `<button type="button" class="name-action-btn" data-action="start" data-id="${{esc(c.id)}}" data-name="${{esc(c.name)}}" title="Iniciar" aria-label="Iniciar ${{esc(c.name)}}">${{PLAY_ICON}}</button>`;
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
          <div class="container-actions">
            ${{lifecycleBtn}}
            <button type="button" class="name-action-btn" data-action="restart" data-id="${{esc(c.id)}}" data-name="${{esc(c.name)}}" title="Reiniciar" aria-label="Reiniciar ${{esc(c.name)}}">${{RESTART_ICON}}</button>
            <button type="button" class="name-action-btn delete-btn" data-action="delete" data-id="${{esc(c.id)}}" data-name="${{esc(c.name)}}" title="Apagar" aria-label="Apagar ${{esc(c.name)}}">${{TRASH_ICON}}</button>
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
            ${{renderStackActions(stack)}}
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
      if (document.getElementById("settings-modal").hidden && document.getElementById("meter-modal").hidden) {{
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

    function clearLogs() {{
      const view = document.getElementById("logs-view");
      if (!view.textContent) return;
      view.textContent = "";
      logsStickBottom = true;
      document.getElementById("logs-progress").hidden = true;
      document.getElementById("logs-progress-bar").style.width = "0%";
      document.getElementById("logs-scroll").scrollTop = 0;
    }}

    document.getElementById("q").addEventListener("input", (e) => {{
      state.query = e.target.value;
      render();
    }});

    document.querySelectorAll(".sort-btn").forEach((btn) => {{
      btn.addEventListener("click", () => toggleSort(btn.dataset.key));
    }});

    document.getElementById("meters").addEventListener("click", (e) => {{
      const expand = e.target.closest("[data-meter-detail]");
      if (expand) {{
        openMeterDetail(expand.dataset.meterDetail, expand.dataset.meterLabel || expand.dataset.meterDetail);
        return;
      }}
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

    async function containerAction(id, name, action) {{
      if (action === "delete") {{
        if (!confirm(`Apagar o container "${{name}}"? Esta ação não pode ser desfeita.`)) return;
      }}
      const url = action === "delete"
        ? `/api/containers/${{encodeURIComponent(id)}}`
        : `/api/containers/${{encodeURIComponent(id)}}/${{action}}`;
      try {{
        const res = await fetch(url, {{
          method: action === "delete" ? "DELETE" : "POST",
          cache: "no-store",
        }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
        refreshing = false;
        await refresh();
      }} catch (err) {{
        alert("Falha ao executar ação: " + (err && err.message ? err.message : err));
      }}
    }}

    async function stackAction(stackName, action) {{
      if (action === "delete") {{
        if (!confirm(`Apagar todos os containers da stack "${{stackName}}"? Esta ação não pode ser desfeita.`)) return;
      }}
      const enc = encodeURIComponent(stackName);
      const url = action === "delete"
        ? `/api/stacks/${{enc}}`
        : `/api/stacks/${{enc}}/${{action}}`;
      try {{
        const res = await fetch(url, {{
          method: action === "delete" ? "DELETE" : "POST",
          cache: "no-store",
        }});
        const data = await res.json().catch(() => ({{}}));
        if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
        refreshing = false;
        await refresh();
      }} catch (err) {{
        alert("Falha ao executar ação na stack: " + (err && err.message ? err.message : err));
      }}
    }}

    function handleStacksClick(e) {{
      const stackActionBtn = e.target.closest("[data-stack-action]");
      if (stackActionBtn) {{
        stackAction(stackActionBtn.dataset.stackName, stackActionBtn.dataset.stackAction);
        return;
      }}
      const actionBtn = e.target.closest("[data-action]");
      if (actionBtn) {{
        containerAction(actionBtn.dataset.id, actionBtn.dataset.name, actionBtn.dataset.action);
        return;
      }}
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

    const fullscreenToggle = document.getElementById("fullscreen-toggle");
    if (document.fullscreenEnabled && fullscreenToggle) {{
      fullscreenToggle.addEventListener("click", toggleFullscreen);
      document.addEventListener("fullscreenchange", updateFullscreenButton);
    }} else if (fullscreenToggle) {{
      fullscreenToggle.hidden = true;
    }}

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

    document.getElementById("meter-close").addEventListener("click", closeMeterDetail);
    document.getElementById("meter-prev").addEventListener("click", () => navigateMeterDetail(-1));
    document.getElementById("meter-next").addEventListener("click", () => navigateMeterDetail(1));
    document.getElementById("meter-modal").addEventListener("click", (e) => {{
      if (e.target.id === "meter-modal") closeMeterDetail();
    }});

    document.getElementById("logs-close").addEventListener("click", closeLogs);
    document.getElementById("logs-copy").addEventListener("click", copyLogs);
    document.getElementById("logs-clear").addEventListener("click", clearLogs);
    document.getElementById("logs-modal").addEventListener("click", (e) => {{
      if (e.target.id === "logs-modal") closeLogs();
    }});
    document.getElementById("logs-scroll").addEventListener("scroll", updateScrollProgress);
    document.addEventListener("keydown", (e) => {{
      if (e.key !== "Escape") return;
      if (!document.getElementById("settings-modal").hidden) closeSettings();
      else if (!document.getElementById("meter-modal").hidden) closeMeterDetail();
      else if (!document.getElementById("logs-modal").hidden) closeLogs();
    }});

    document.addEventListener("visibilitychange", () => {{
      if (document.hidden) return;
      refresh();
      if (meterDetailKind === "cpu" && !document.getElementById("meter-modal").hidden) {{
        loadMeterDetail("cpu");
      }}
    }});

    loadPrefs().then(() => {{
      applySettings();
      setClock();
      renderMeters();
      recordUsageSamples();
      render();
      setInterval(refresh, REFRESH_MS);
    }}).catch((e) => {{
      const err = document.getElementById("error");
      err.hidden = false;
      err.textContent = "Falha ao carregar preferências: " + (e && e.message ? e.message : e);
    }});
  </script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON inválido") from exc
        if not isinstance(data, dict):
            raise ValueError("Corpo deve ser um objeto JSON")
        return data

    def do_GET(self) -> None:  # noqa: N802
        reload_if_stale()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        host = request_host(self)

        if path == "/api/prefs":
            self._send_json(get_prefs())
            return

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

        if path.startswith("/api/meters/"):
            kind = urllib.parse.unquote(path[len("/api/meters/") :]).strip("/")
            try:
                payload = meter_detail_payload(kind)
                status = 200
            except ValueError as exc:
                payload = {"kind": kind, "error": str(exc)}
                status = 400
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
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

    def do_PUT(self) -> None:  # noqa: N802
        reload_if_stale()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/prefs":
            self.send_error(404, "Not Found")
            return
        try:
            data = self._read_json_body()
            self._send_json(update_prefs(data))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def _handle_container_action(self, ref: str, action: str) -> None:
        try:
            if action == "start":
                container_start(ref)
            elif action == "stop":
                container_stop(ref)
            elif action == "restart":
                container_restart(ref)
            else:
                self.send_error(404, "Not Found")
                return
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=502)

    def _handle_stack_action(self, stack: str, action: str) -> None:
        try:
            if action == "stop":
                stack_stop(stack)
            elif action == "restart":
                stack_restart(stack)
            else:
                self.send_error(404, "Not Found")
                return
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=502)

    def do_POST(self) -> None:  # noqa: N802
        reload_if_stale()
        parsed = urllib.parse.urlparse(self.path)
        stack_match = re.fullmatch(r"/api/stacks/([^/]+)/(stop|restart)", parsed.path)
        if stack_match:
            stack = urllib.parse.unquote(stack_match.group(1))
            self._handle_stack_action(stack, stack_match.group(2))
            return
        match = re.fullmatch(r"/api/containers/([^/]+)/(start|stop|restart)", parsed.path)
        if not match:
            self.send_error(404, "Not Found")
            return
        ref = urllib.parse.unquote(match.group(1))
        self._handle_container_action(ref, match.group(2))

    def do_DELETE(self) -> None:  # noqa: N802
        reload_if_stale()
        parsed = urllib.parse.urlparse(self.path)
        stack_match = re.fullmatch(r"/api/stacks/([^/]+)", parsed.path)
        if stack_match:
            stack = urllib.parse.unquote(stack_match.group(1))
            try:
                stack_remove(stack)
                self._send_json({"ok": True})
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=502)
            return
        match = re.fullmatch(r"/api/containers/([^/]+)", parsed.path)
        if not match:
            self.send_error(404, "Not Found")
            return
        ref = urllib.parse.unquote(match.group(1))
        force = urllib.parse.parse_qs(parsed.query).get("force", ["true"])[0].lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            container_remove(ref, force=force)
            self._send_json({"ok": True})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=502)

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
    init_db()
    cache = get_metrics_cache()
    cache.warm()
    cache.start()

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
