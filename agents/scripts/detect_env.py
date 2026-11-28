#!/usr/bin/env python3
"""Zero-dependency host environment inspector for Relay setup agents.

Emits a structured JSON report describing OS, init system, installed tools,
VPN interfaces, LAN addresses, storage availability, and recommended setup profile.
Never reads private keys, passwords, or VPN credential files.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


def _cmd_exists(name: str) -> str | None:
    return shutil.which(name)


def _detect_init_system() -> str:
    if os.name == 'nt':
        return 'windows'
    if sys.platform == 'darwin':
        return 'launchd'
    if Path('/run/systemd/system').is_dir() and _cmd_exists('systemctl'):
        return 'systemd'
    if _cmd_exists('rc-service') or Path('/sbin/openrc-run').exists():
        return 'openrc'
    if Path('/etc/init.d').is_dir():
        return 'sysvinit'
    return 'unknown'


def _detect_distro() -> str:
    if os.name == 'nt':
        return f'Windows {platform.release()}'
    if sys.platform == 'darwin':
        return f'macOS {platform.mac_ver()[0]}'
    os_release = Path('/etc/os-release')
    if os_release.is_file():
        for line in os_release.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('PRETTY_NAME='):
                return line.split('=', 1)[1].strip().strip('"')
    return platform.platform()


def _detect_vpn_interfaces() -> list[str]:
    found: list[str] = []
    try:
        if sys.platform.startswith('linux') and _cmd_exists('ip'):
            out = subprocess.check_output(['ip', '-brief', 'link'], text=True, stderr=subprocess.DEVNULL, timeout=5)
            for line in out.splitlines():
                name = line.split()[0].split('@')[0] if line.split() else ''
                if name.startswith(('wg', 'tun', 'tap', 'proton', 'mullvad', 'tailscale', 'nordlynx')):
                    found.append(name)
        elif sys.platform == 'darwin' and _cmd_exists('ifconfig'):
            out = subprocess.check_output(['ifconfig', '-l'], text=True, stderr=subprocess.DEVNULL, timeout=5)
            for name in out.split():
                if name.startswith(('utun', 'wg', 'tun', 'tap')):
                    found.append(name)
        elif os.name == 'nt':
            out = subprocess.check_output(['netsh', 'interface', 'show', 'interface'], text=True,
                                          stderr=subprocess.DEVNULL, timeout=5)
            for line in out.splitlines():
                lower = line.lower()
                if any(k in lower for k in ('wireguard', 'proton', 'mullvad', 'openvpn', 'tailscale', 'wintun')):
                    parts = line.split(None, 3)
                    if len(parts) == 4:
                        found.append(parts[3].strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(set(found))


def _detect_lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('1.1.1.1', 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.'):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def _check_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('0.0.0.0', port))
            return True
        except OSError:
            return False


def _storage_candidates() -> list[dict[str, Any]]:
    candidates = [Path.cwd(), Path.home()]
    if os.name != 'nt':
        for extra in ('/srv', '/mnt', '/media', '/var/lib'):
            p = Path(extra)
            if p.is_dir():
                candidates.append(p)
    seen: set[str] = set()
    results = []
    for path in candidates:
        try:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            usage = shutil.disk_usage(resolved)
            results.append({
                'path': resolved,
                'free_gb': round(usage.free / (1024 ** 3), 1),
                'total_gb': round(usage.total / (1024 ** 3), 1),
            })
        except OSError:
            continue
    return results


def _recommend_profile(init_sys: str, tools: dict[str, str | None], vpn_ifaces: list[str]) -> str:
    if sys.platform.startswith('linux'):
        has_guard_stack = bool(tools.get('nft') and tools.get('ip') and vpn_ifaces)
        if has_guard_stack and init_sys == 'systemd':
            return 'linux-systemd-vpn'
        if has_guard_stack and init_sys == 'openrc':
            return 'linux-openrc-vpn'
        if tools.get('docker'):
            return 'docker-gluetun'
    return 'standalone-desktop'


def _detect_ram_gb() -> float | None:
    try:
        if os.name == 'nt':
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_uint32),
                    ('dwMemoryLoad', ctypes.c_uint32),
                    ('ullTotalPhys', ctypes.c_uint64),
                    ('ullAvailPhys', ctypes.c_uint64),
                    ('ullTotalPageFile', ctypes.c_uint64),
                    ('ullAvailPageFile', ctypes.c_uint64),
                    ('ullTotalVirtual', ctypes.c_uint64),
                    ('ullAvailVirtual', ctypes.c_uint64),
                    ('ullAvailExtendedVirtual', ctypes.c_uint64),
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullTotalPhys / (1024 ** 3), 1)
        elif sys.platform.startswith('linux'):
            meminfo = Path('/proc/meminfo')
            if meminfo.is_file():
                for line in meminfo.read_text(encoding='utf-8', errors='replace').splitlines():
                    if line.startswith('MemTotal:'):
                        return round(int(line.split()[1]) / (1024 ** 2), 1)
        elif sys.platform == 'darwin':
            raw = subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=2).strip()
            return round(int(raw) / (1024 ** 3), 1)
    except Exception:
        return None
    return None


def main() -> None:
    tool_names = [
        'aria2c', 'uv', 'pip', 'docker', 'nft', 'ip', 'nmcli',
        'wg', 'wg-quick', 'tailscale', 'caddy', 'nginx', 'pyinstaller',
    ]
    tools = {name: _cmd_exists(name) for name in tool_names}
    init_sys = _detect_init_system()
    vpn_ifaces = _detect_vpn_interfaces()
    cpu_count = os.cpu_count() or 1
    ram_gb = _detect_ram_gb()
    perf_profile = 'fast' if (ram_gb is not None and ram_gb >= 12.0 and cpu_count >= 6) else 'balanced'
    report = {
        'os': os.name,
        'platform': sys.platform,
        'distro': _detect_distro(),
        'arch': platform.machine(),
        'cpu_threads': cpu_count,
        'ram_gb': ram_gb,
        'hostname': socket.gethostname(),
        'python_version': platform.python_version(),
        'is_admin': (os.geteuid() == 0) if hasattr(os, 'geteuid') else False,
        'init_system': init_sys,
        'tools': tools,
        'vpn_interfaces': vpn_ifaces,
        'lan_ips': _detect_lan_ips(),
        'ports_available': {str(p): _check_port(p) for p in (80, 8080, 16874)},
        'storage': _storage_candidates(),
        'recommended_profile': _recommend_profile(init_sys, tools, vpn_ifaces),
        'recommended_performance_profile': perf_profile,
    }
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
