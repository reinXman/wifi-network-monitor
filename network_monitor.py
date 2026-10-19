#!/usr/bin/env python3
"""
WiFi / Network Monitor — a professional, dark-themed live network dashboard.

Zero third-party dependencies. Pure Python standard library (tkinter + ctypes).

What it monitors:
  * LAN latency   — ICMP pings to your default gateway (router health)
  * WAN latency   — ICMP pings to an internet host (default 1.1.1.1)
  * Jitter        — variation in WAN latency over the window
  * Packet loss   — failed probes (per-window and per-session)
  * DNS latency   — real raw-UDP DNS query round-trip time
  * WiFi details  — SSID, signal %, channel, link speed (needs elevation
                    or Location permission on Windows 11; degrades gracefully)

Separating LAN from WAN tells you WHERE the problem is:
    LAN ok  + WAN ok   ->  ONLINE
    LAN ok  + WAN bad  ->  "No internet" (router fine, ISP/VPN issue)
    LAN bad            ->  OFFLINE (router/cable/wifi problem)

Usage:
  python network_monitor.py                 # launch dashboard
  python network_monitor.py --interval 2    # probe every 2 s
  python network_monitor.py --wan 9.9.9.9   # different WAN probe host
  python network_monitor.py --no-gui        # headless diagnostic mode (5 s)
"""

import argparse
import csv
import ctypes
import os
import re
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque, namedtuple

# ---------------------------------------------------------------------------
# Platform helpers (Windows-first, graceful elsewhere)
# ---------------------------------------------------------------------------

IS_WINDOWS = sys.platform == "win32"
# Prevent console windows when running under pythonw (windowless launcher):
# console subprocesses (route, netsh, …) would otherwise flash a new cmd window
# on every call. 0 is a no-op on POSIX and inside console sessions.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---- ICMP via iphlpapi (no ping.exe parsing, locale-independent) -----------

if IS_WINDOWS:
    _icmp = ctypes.windll.iphlpapi
    _icmp.IcmpCreateFile.restype = ctypes.c_void_p
    _icmp.IcmpSendEcho.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint16,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
    ]
    _icmp.IcmpCloseHandle.argtypes = [ctypes.c_void_p]

    class _ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", ctypes.c_uint32), ("Status", ctypes.c_uint32),
                    ("RoundTripTime", ctypes.c_uint32), ("DataSize", ctypes.c_uint16),
                    ("Reserved", ctypes.c_uint16), ("Data", ctypes.c_void_p),
                    ("Options", ctypes.c_void_p)]


def icmp_ping(ip_str: str, timeout_ms: int = 1500, payload: bytes = b"network-monitor-32-byte-payload!") -> float | None:
    """Send one ICMP echo, return RTT in ms, or None on failure/timeout."""
    if not IS_WINDOWS:
        try:
            out = subprocess.run(
                ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip_str],
                capture_output=True, text=True, timeout=timeout_ms / 1000 + 1)
            m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out.stdout, re.I)
            return float(m.group(1)) if m else None
        except Exception:
            return None
    h = _icmp.IcmpCreateFile()
    try:
        dest = struct.unpack("<I", socket.inet_aton(ip_str))[0]
        buf = ctypes.create_string_buffer(1024)
        data = ctypes.create_string_buffer(payload, len(payload))
        n = _icmp.IcmpSendEcho(ctypes.c_void_p(h), dest, data, len(payload),
                               None, buf, 1024, timeout_ms)
        if not n:
            return None
        reply = ctypes.cast(buf, ctypes.POINTER(_ICMP_ECHO_REPLY)).contents
        if reply.Status != 0:  # 0 == IP_SUCCESS
            return None
        return float(reply.RoundTripTime)
    except Exception:
        return None
    finally:
        _icmp.IcmpCloseHandle(ctypes.c_void_p(h))


def resolve_host(host: str) -> str | None:
    """Return an IPv4 address for a target (IP passthrough or DNS lookup)."""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host or ""):
        return host
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except Exception:
        return None


def tcp_probe(ip: str, port: int = 443, timeout: float = 1.5) -> float | None:
    """TCP connect RTT in ms, or None. Cross-check for ICMP-blocked paths."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.connect((ip, port))
        return (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None
    finally:
        s.close()


# ---- Load testing: a mini-iperf in pure stdlib, plus a rude UDP blaster ----

DEFAULT_LOAD_PORT = 5215
LOAD_MAGIC = "NMLOAD/1"
MAX_LOAD_SECONDS = 300.0
_CHUNK = 256 * 1024


class _Rate:
    """Shared token-bucket rate limiter (bytes across all streams). 0 = flat out."""

    def __init__(self, mbps: float):
        self.rate = mbps * 1e6 / 8.0 if mbps and mbps > 0 else 0.0
        self.sent = 0
        self.t0 = None
        self.lock = threading.Lock()

    def wait(self, n: int):
        if not self.rate:
            return
        with self.lock:
            if self.t0 is None:
                self.t0 = time.perf_counter()
            self.sent += n
            over = self.sent - self.rate * (time.perf_counter() - self.t0)
        if over > 0:
            time.sleep(min(0.1, over / self.rate))


def load_server(port: int = DEFAULT_LOAD_PORT) -> None:
    """Act as the far end of a load test (blocking; Ctrl+C to stop).

    Deploy this on any machine across the path you want to fill: run
    `python network_monitor.py --serve`, open the port, and point a client
    at it. Protocol: each connection sends 'NMLOAD/1 <up|down> <seconds>\n'.
      up   -> server drains bytes until EOF or the deadline
      down -> server streams a preallocated buffer until the deadline
    A client opens several connections in parallel to fill the pipe.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(16)
    print(f"load-test server listening on 0.0.0.0:{port} (Ctrl+C to stop)", flush=True)
    payload = os.urandom(_CHUNK)

    def handle(conn: socket.socket):
        try:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf and len(buf) < 128:
                part = conn.recv(64)
                if not part:
                    return
                buf += part
            parts = buf.split(b"\n", 1)[0].decode("ascii", "ignore").split()
            if len(parts) != 3 or parts[0] != LOAD_MAGIC:
                return
            direction, seconds = parts[1], float(parts[2])
            if direction not in ("up", "down") or not 0 < seconds <= MAX_LOAD_SECONDS:
                return
            conn.settimeout(30)
            if direction == "up":
                deadline = time.monotonic() + seconds + 2.0  # grace for in-flight data
                while time.monotonic() < deadline:
                    if not conn.recv(_CHUNK):
                        return
            else:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    conn.sendall(payload)
        except OSError:
            pass  # client hung up / deadline hit — that's the normal exit
        finally:
            try:
                conn.close()
            except OSError:
                pass

    try:
        while True:
            conn, _addr = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    finally:
        srv.close()


def tcp_load_client(host: str, port: int = DEFAULT_LOAD_PORT, seconds: float = 15.0,
                    streams: int = 4, direction: str = "both", mbps: float = 0.0) -> dict:
    """Fill the path to `host` with `streams` parallel TCP streams.

    Requires a peer running --serve across the path being tested. Returns
    achieved Mbps per direction, e.g. {"down": 94.2, "up": 11.5} (None = failed).
    """
    results: dict = {}

    def stream(d: str):
        rate = _Rate(mbps)
        payload = os.urandom(_CHUNK)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sent = 0
        try:
            s.settimeout(30)
            s.connect((host, port))
            s.sendall(f"{LOAD_MAGIC} {d} {seconds}\n".encode())
            if d == "up":
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    s.sendall(payload)
                    sent += len(payload)
                    rate.wait(len(payload))
            else:
                end = time.monotonic() + seconds
                s.settimeout(2.0)
                while time.monotonic() < end:
                    try:
                        got = s.recv(_CHUNK)
                    except socket.timeout:
                        continue
                    if not got:
                        break
                    sent += len(got)
            results[d] = sent * 8.0 / 1e6 / seconds
        except OSError as e:
            results[d] = None
            results["error"] = str(e)
        finally:
            try:
                s.close()
            except OSError:
                pass

    def wave(d: str):
        threads = [threading.Thread(target=stream, args=(d,), daemon=True)
                   for _ in range(max(1, streams))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    if direction in ("down", "both"):
        wave("down")
    if direction in ("up", "both"):
        wave("up")
    return results


def udp_blast(host: str, port: int = 9, seconds: float = 10.0, mbps: float = 20.0,
              size: int = 1400) -> float | None:
    """"Rude" load: fire-and-forget UDP blast. No peer cooperation needed.

    The point is to fill the *outbound* path with junk — useful when you can't
    deploy a peer, or want to stress the link itself. Etiquette and sanity:
      * point it at a discarded/filtered port (9=discard, or a firewalled port)
        so the target doesn't answer with ICMP unreachable storms
      * keep the rate cap honest — this is a troubleshooting tool, not a weapon
    Returns achieved Mbps, or None on a network error.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rate = _Rate(mbps)
    payload = os.urandom(max(1, min(size, 65507)))
    sent, t0 = 0, time.perf_counter()
    try:
        while time.perf_counter() - t0 < seconds:
            s.sendto(payload, (host, port))
            sent += len(payload)
            rate.wait(len(payload))
        return sent * 8.0 / 1e6 / seconds
    except OSError:
        return None
    finally:
        s.close()


def bufferbloat(series: list, t0: float, t1: float, idle: float = 30.0):
    """Mean latency before vs during [t0, t1]. Returns (idle_mean|None, load_mean)."""
    pre = [s.v for s in series if s.v is not None and t0 - idle <= s.t < t0]
    load = [s.v for s in series if s.v is not None and t0 <= s.t <= t1]
    if not load:
        return None
    return (statistics.fmean(pre) if pre else None, statistics.fmean(load))


def cli_load_test(host: str, *, seconds: float, streams: int, direction: str,
                  mbps: float, rude: bool, rude_port: int) -> None:
    """Headless load test: also pings the target 1x/s and reports bufferbloat."""
    ip = resolve_host(host)
    if not ip:
        print(f"cannot resolve {host}")
        return
    samples: list[tuple[float, float | None]] = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            samples.append((time.time(), icmp_ping(ip, timeout_ms=1000)))
            stop.wait(1.0)

    threading.Thread(target=sampler, daemon=True).start()
    print(f"target {ip} - 5 s idle baseline...")
    time.sleep(5.0)
    t0 = time.time()
    if rude:
        got = udp_blast(ip, rude_port, seconds, mbps or 20.0)
        print(f"UDP blast: {got:.1f} Mbps sent" if got else "UDP blast failed (network error)")
    else:
        res = tcp_load_client(ip, seconds=seconds, streams=streams,
                              direction=direction, mbps=mbps)
        for d, label in (("down", "Download"), ("up", "Upload")):
            if res.get(d) is not None:
                print(f"{label:9}: {res[d]:8.1f} Mbps")
            else:
                extra = f" ({res['error']})" if res.get("error") else ""
                print(f"{label:9}: failed{extra}")
    t1 = time.time()
    time.sleep(2.0)  # let the queue drain before the last samples
    stop.set()
    pre = [v for t, v in samples if v is not None and t < t0]
    load = [v for t, v in samples if v is not None and t0 <= t <= t1]
    if pre and load:
        pi, li = statistics.fmean(pre), statistics.fmean(load)
        print(f"latency idle->loaded: {pi:.1f} -> {li:.1f} ms  "
              f"(bufferbloat +{li - pi:.1f} ms)")
    elif load:
        print(f"latency under load: {statistics.fmean(load):.1f} ms (no idle baseline)")
    else:
        print("no successful pings — target unreachable or fully saturated")


# ---- Raw UDP DNS probe (same wire-craft as our DNS benchmark) --------------

def _build_query(name: str) -> tuple[int, bytes]:
    qid = int.from_bytes(os.urandom(2), "big")
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(l)]) + l.encode("ascii") for l in name.split(".") if l) + b"\x00"
    return qid, header + qname + struct.pack(">HH", 1, 1)


def dns_probe(server: str = "1.1.1.1", name: str = "www.google.com", timeout: float = 2.0) -> float | None:
    """Time a real A-record query over UDP. Returns RTT in ms or None."""
    qid, pkt = _build_query(name)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(1024)
        if len(data) < 12 or data[:2] != struct.pack(">H", qid):
            return None
        flags = (data[2] << 8) | data[3]
        if not flags & 0x8000:  # QR bit: not a response
            return None
        if flags & 0x000F:      # RCODE != NOERROR
            return None
        return (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None
    finally:
        s.close()


# ---- System DNS servers via GetNetworkParams (locale-free, no admin) -------

class _IP_ADDR_STRING(ctypes.Structure):
    _fields_ = [("Next", ctypes.c_void_p), ("IpAddress", ctypes.c_char * 16),
                ("IpMask", ctypes.c_char * 16), ("Context", ctypes.c_uint32)]


class _FIXED_INFO_HEAD(ctypes.Structure):
    # HostName[132] + DomainName[132] + PIP_ADDR_STRING + IP_ADDR_STRING list
    _fields_ = [("HostName", ctypes.c_char * 132), ("DomainName", ctypes.c_char * 132),
                ("CurrentDnsServer", ctypes.c_void_p),
                ("DnsServerList", _IP_ADDR_STRING)]


def get_dns_servers() -> list[str]:
    """Return configured DNS server IPs (IPv4), best-effort."""
    if not IS_WINDOWS:
        try:
            out = subprocess.run(["resolvectl", "status"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW).stdout
            return list(dict.fromkeys(re.findall(r"DNS Servers?: (\d+\.\d+\.\d+\.\d+)", out)))
        except Exception:
            return []
    try:
        buf = ctypes.create_string_buffer(4096)
        size = ctypes.c_uint(4096)
        if ctypes.windll.iphlpapi.GetNetworkParams(buf, ctypes.byref(size)) != 0:
            return []
        fi = ctypes.cast(buf, ctypes.POINTER(_FIXED_INFO_HEAD)).contents
        out, node = [], ctypes.cast(ctypes.byref(fi.DnsServerList), ctypes.c_void_p)
        while node:
            ips = ctypes.cast(node, ctypes.POINTER(_IP_ADDR_STRING)).contents
            ip = ips.IpAddress.decode("ascii", "ignore").strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                out.append(ip)
            node = ips.Next
        return out
    except Exception:
        return []


# ---- Default gateway (parse `route print`; IP literals are locale-free) ----

def get_default_gateway() -> str | None:
    if not IS_WINDOWS:
        try:
            out = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True, creationflags=NO_WINDOW).stdout
            m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
            return m.group(1) if m else None
        except Exception:
            return None
    try:
        out = subprocess.run(["route", "print", "-4", "0.0.0.0"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout
        rows = re.findall(
            r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+(\S+)\s+(\d+)\s*$", out, re.M)
        best_ip, best_metric = None, None
        for gw, _iface, metric in rows:
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", gw):
                continue  # "On-link" (VPN tunnel) — not pingeable
            m = int(metric)
            if best_metric is None or m < best_metric:
                best_ip, best_metric = gw, m
        return best_ip
    except Exception:
        return None


# ---- WiFi details (best effort; needs elevation / Location on Win11) -------

def query_wifi_native() -> dict | None:
    """Read WiFi state via wlanapi.dll (locale-free). Needs elevation on Win 11.

    Layout verified empirically against netsh ground truth (Win 11; the
    documented struct differs): isState@0, strProfileName@8 (512 B), then the
    connection DOT11_SSID (located by pattern-scan for robustness), followed
    by dot11BssType@+0, BSSID@+4 (6 B), dot11BssPhyType@+48, ulPhyId@+52,
    wlanSignalQuality@+56, rx/tx rate in kbps@+60/+64 — relative to the SSID
    offset. Channel is not present; band is derived from the PHY type.
    """
    if not IS_WINDOWS:
        return None
    try:
        wlan = ctypes.windll.wlanapi
        h = ctypes.c_void_p()
        ver = ctypes.c_uint32(2)
        if wlan.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h)) != 0:
            return None
        try:
            plist = ctypes.c_void_p()
            # fills a WLAN_INTERFACE_INFO_LIST: DWORD dwNumberOfItems, DWORD dwIndex, entries
            if wlan.WlanEnumInterfaces(h, None, ctypes.byref(plist)) != 0 or not plist.value:
                return None
            count = int.from_bytes(ctypes.string_at(plist.value, 4), "little")
            info_size = 16 + 512 + 4  # GUID + WCHAR[256] + enum (WLAN_INTERFACE_INFO)
            best = None
            for i in range(count):
                guid = (ctypes.c_char * 16).from_buffer_copy(
                    ctypes.string_at(plist.value + 8 + i * info_size, 16))
                size, data = ctypes.c_uint32(), ctypes.c_void_p()
                # opcode 7 = wlan_intf_opcode_current_connection
                if wlan.WlanQueryInterface(h, guid, 7, None, ctypes.byref(size),
                                           ctypes.byref(data), None) != 0 or not data.value:
                    continue
                try:
                    raw = ctypes.string_at(data.value, size.value)
                    if len(raw) < 120:
                        continue
                    if int.from_bytes(raw[0:4], "little") != 1:  # not connected
                        continue
                    # locate the connection DOT11_SSID: uSSIDLength (1..32) followed
                    # by printable ASCII and a NUL (scan backwards past profile name)
                    ssid_off = None
                    for off in range(min(len(raw) - 40, 600), 8, -4):
                        n = int.from_bytes(raw[off:off + 4], "little")
                        if 1 <= n <= 32:
                            chunk = raw[off + 4:off + 4 + n]
                            if all(32 <= c < 127 for c in chunk) and raw[off + 4 + n] == 0:
                                ssid_off = off
                                break
                    if ssid_off is None:
                        continue
                    info = {"ssid": raw[ssid_off + 4:ssid_off + 4 +
                                        int.from_bytes(raw[ssid_off:ssid_off + 4], "little")
                                        ].decode("ascii")}
                    sig = int.from_bytes(raw[ssid_off + 56:ssid_off + 60], "little")
                    if not 0 <= sig <= 100:
                        continue
                    info["signal"] = sig
                    phy = int.from_bytes(raw[ssid_off + 48:ssid_off + 52], "little")
                    if phy in (1, 2, 5, 6):
                        info["band"] = "2.4 GHz"
                    elif phy in (4, 7, 8):
                        info["band"] = "5 GHz"
                    elif phy == 9:
                        info["band"] = "6 GHz"
                    rx = int.from_bytes(raw[ssid_off + 60:ssid_off + 64], "little")
                    tx = int.from_bytes(raw[ssid_off + 64:ssid_off + 68], "little")
                    if 1_000 <= rx <= 10_000_000:
                        info["rx"] = f"{rx / 1000:g} Mbps"
                    if 1_000 <= tx <= 10_000_000:
                        info["tx"] = f"{tx / 1000:g} Mbps"
                    best = info
                finally:
                    wlan.WlanFreeMemory(ctypes.c_void_p(data.value))
            return best
        finally:
            wlan.WlanCloseHandle(h, None)
    except Exception:
        return None


def query_wifi() -> dict | None:
    """Best-effort WiFi info: native API first, netsh as fallback."""
    native = query_wifi_native()
    if native:
        return native
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=NO_WINDOW).stdout
    except Exception:
        return None
    low = out.lower()
    if not out.strip() or "no wireless interface" in low or "location permission" in low \
            or "requires elevation" in low or "returns error" in low:
        return None
    info = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "ssid" or (key.endswith("ssid") and "bssid" not in key):
            info["ssid"] = val
        elif "signal" in key and "%" in val:
            try:
                info["signal"] = int(re.search(r"(\d+)", val).group(1))
            except Exception:
                pass
        elif "channel" in key:
            info["channel"] = val
        elif "band" in key:
            info["band"] = val
        elif "receive rate" in key:
            info["rx"] = val
        elif "transmit rate" in key:
            info["tx"] = val
        elif key == "description":
            info["adapter"] = val
    return info or None


# ---------------------------------------------------------------------------
# Shared state + collector thread
# ---------------------------------------------------------------------------

Sample = namedtuple("Sample", "t v")


class State:
    def __init__(self, window: int):
        self.lock = threading.Lock()
        self.window = window
        self.lan = deque(maxlen=window)
        self.wan = deque(maxlen=window)
        self.extra = deque(maxlen=window)
        self.dns = deque(maxlen=window)
        self.signal = deque(maxlen=window)
        self.events = deque(maxlen=100)
        self.load_spans = deque(maxlen=50)  # [t0, t1|None, label] load-test windows
        self.load_active = False
        self.wifi = None
        self.gateway = None
        self.wan_host = None
        self.extra_host = None
        self.wifi_available = None  # None = unknown, False = unavailable
        self.dns_server = None
        self.start_time = time.time()
        self.counts = {"wan_sent": 0, "wan_recv": 0, "lan_sent": 0, "lan_recv": 0,
                       "extra_sent": 0, "extra_recv": 0}
        self._lan_ok = self._wan_ok = self._extra_ok = None
        self._signal_warned = False

    def add_event(self, msg: str, kind: str = "info"):
        self.events.append(Sample(time.strftime("%H:%M:%S"), (msg, kind)))

    def record(self, deq, v):
        with self.lock:
            deq.append(Sample(time.time(), v))

    def check_transitions(self, lan_ok: bool, wan_ok: bool,
                          extra_ok: bool | None = None,
                          extra_label: str | None = None):
        if self._lan_ok is not None:
            if self._lan_ok and not lan_ok:
                self.add_event("Connection to router LOST", "bad")
            elif not self._lan_ok and lan_ok:
                self.add_event("Router connection restored", "good")
            # if the whole link dropped, the LAN-lost event already covers it
            if self._wan_ok and not wan_ok and lan_ok:
                self.add_event("Internet connectivity LOST", "bad")
            elif not self._wan_ok and wan_ok and self._lan_ok:
                self.add_event("Internet connectivity restored", "good")
        if extra_ok is not None and self._extra_ok is not None and extra_label:
            if self._extra_ok and not extra_ok:
                self.add_event(f"Custom target {extra_label} LOST", "bad")
            elif not self._extra_ok and extra_ok:
                self.add_event(f"Custom target {extra_label} restored", "good")
        self._lan_ok, self._wan_ok = lan_ok, wan_ok
        self._extra_ok = extra_ok if extra_ok is not None else self._extra_ok


class Collector(threading.Thread):
    def __init__(self, state: State, interval: float, wan_host: str, dns_server: str,
                 dns_name: str, extra_host: str | None = None):
        super().__init__(daemon=True)
        self.state = state
        self.interval = interval
        self.wan_host = wan_host
        self.extra_host = extra_host
        self.dns_server = dns_server
        self.dns_name = dns_name
        self.stop_flag = threading.Event()
        self._tick = 0
        self._began = False
        self._wan_ip = None    # resolved IPv4 for the WAN target
        self._extra_ip = None  # resolved IPv4 for the custom target
        self._last_tcp_check = 0.0
        # CSV logging
        self.log_flag = threading.Event()
        self.log_file = None
        self.log_path = None
        self._last_dns = None
        self._last_signal = None

    def _resolve_targets(self, st: State) -> None:
        """Resolve hostname targets to IPs (no-op for IP literals)."""
        ip = resolve_host(self.wan_host)
        if ip:
            self._wan_ip = ip
        if self.extra_host:
            ip = resolve_host(self.extra_host)
            if ip:
                self._extra_ip = ip
        else:
            self._extra_ip = None

    def set_targets(self, wan_host: str, extra_host: str | None) -> bool:
        """Live-update probe targets (thread-safe; called from the GUI)."""
        wan_host = (wan_host or "").strip()
        if not wan_host:
            return False
        st = self.state
        self.wan_host = wan_host
        self._wan_ip = None
        if extra_host != self.extra_host:
            self.extra_host = extra_host
            self._extra_ip = None
            with st.lock:
                st.extra.clear()
        st.wan_host = wan_host
        st.extra_host = extra_host
        self._resolve_targets(st)
        msg = f"Targets set — WAN {wan_host} ({self._wan_ip or 'unresolved'})"
        if extra_host:
            msg += f"; custom {extra_host} ({self._extra_ip or 'unresolved'})"
        st.add_event(msg, "info")
        return True

    def set_dns(self, server: str) -> bool:
        """Live-update the DNS probe target ('auto' = re-detect system DNS)."""
        server = (server or "").strip() or "auto"
        st = self.state
        self.dns_server = server
        if server == "auto":
            st.dns_server = None
            servers = get_dns_servers()
            if servers:
                st.dns_server = servers[0]
            st.add_event(f"DNS probe target: {st.dns_server or '1.1.1.1 (fallback)'} (system)",
                         "info")
        else:
            st.dns_server = server
            ip = resolve_host(server)
            if ip:
                st.add_event(f"DNS probe target: {server}" + (f" ({ip})" if ip != server else ""),
                             "info")
            else:
                st.add_event(f"DNS probe target: {server} — not resolvable yet", "warn")
        return True

    def run(self):
        st = self.state
        if not self._began:
            self._began = True
            st.wan_host = self.wan_host
            st.extra_host = self.extra_host
            st.gateway = get_default_gateway()
            if st.gateway:
                st.add_event(f"Monitoring started — gateway {st.gateway}, WAN host {self.wan_host}")
            else:
                st.add_event("Monitoring started — gateway not found, WAN-only mode")

        while not self.stop_flag.is_set():
            t0 = time.perf_counter()

            if self._tick % 30 == 0:
                gw = get_default_gateway()
                if gw != st.gateway:
                    st.gateway = gw
                    if gw:
                        st.add_event(f"Default gateway changed to {gw}")
                if self.dns_server == "auto":
                    servers = get_dns_servers()
                    if servers and servers[0] != st.dns_server:
                        st.dns_server = servers[0]
                        st.add_event(f"DNS probe target: {st.dns_server} (system)", "info")
                self._resolve_targets(st)  # pick up DNS changes for hostname targets

            if self._wan_ip is None:
                self._resolve_targets(st)

            lan_rtt = icmp_ping(st.gateway) if st.gateway else None
            wan_rtt = icmp_ping(self._wan_ip) if self._wan_ip else None
            extra_rtt = icmp_ping(self._extra_ip) if self._extra_ip else None
            with st.lock:
                st.counts["lan_sent"] += 1
                st.counts["wan_sent"] += 1
                st.counts["lan_recv"] += lan_rtt is not None
                st.counts["wan_recv"] += wan_rtt is not None
                if self.extra_host:
                    st.counts["extra_sent"] += 1
                    st.counts["extra_recv"] += extra_rtt is not None
            st.record(st.lan, lan_rtt)
            st.record(st.wan, wan_rtt)
            if self.extra_host:
                st.record(st.extra, extra_rtt)
            st.check_transitions(lan_rtt is not None, wan_rtt is not None,
                                 extra_rtt is not None if self.extra_host else None,
                                 self.extra_host)

            # cross-check: when ICMP to WAN fails but LAN is fine, try TCP:443
            # (rate-limited) so ICMP-blocking networks don't read as fully down
            if wan_rtt is None and lan_rtt is not None and self._wan_ip \
                    and time.time() - self._last_tcp_check > 60:
                self._last_tcp_check = time.time()
                if tcp_probe(self._wan_ip):
                    st.add_event(f"{self.wan_host}: ICMP failed but TCP:443 reachable "
                                 "— ICMP may be blocked/deprioritized", "warn")

            # DNS probe (every tick — a blank in the CSV then means a real failure)
            server = st.dns_server if (self.dns_server == "auto" and st.dns_server) \
                else (self.dns_server if self.dns_server != "auto" else "1.1.1.1")
            self._last_dns = dns_probe(server, self.dns_name)
            st.record(st.dns, self._last_dns)

            if self._tick % 10 == 5:  # WiFi info refresh every 10th tick
                w = query_wifi()
                st.wifi = w
                if w is None and st.wifi_available is not False:
                    st.wifi_available = False
                    if IS_WINDOWS:
                        st.add_event("WiFi details unavailable — restart as Administrator "
                                     "to enable signal/SSID (use run_admin.bat)", "warn")
                    else:
                        st.add_event("WiFi details unavailable — not supported on this "
                                     "platform", "warn")
                if w and "signal" in w:
                    sig = w["signal"]
                    self._last_signal = sig
                    st.record(st.signal, sig)
                    if sig < 40 and not st._signal_warned:
                        st._signal_warned = True
                        st.add_event(f"Weak WiFi signal ({sig}%)", "warn")
                    elif sig >= 45:
                        st._signal_warned = False

            self._tick += 1
            self._log_row(lan_rtt, wan_rtt, extra_rtt)
            elapsed = time.perf_counter() - t0
            self.stop_flag.wait(max(0.05, self.interval - elapsed))

    # ---- CSV logging (single-writer: only the collector thread touches the file) ----

    @staticmethod
    def _logs_dir() -> str:
        """Prefer a 'logs' folder next to this script; fall back to user profile.
        Never trust the CWD (can be System32 or a protected folder)."""
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
            os.path.join(os.path.expanduser("~"), "NetworkMonitorLogs"),
        ]
        for d in candidates:
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                continue
        raise OSError("could not create a writable logs directory")

    def start_logging(self, logs_dir: str | None = None) -> str | None:
        """Open a timestamped CSV file and begin writing one row per probe."""
        try:
            logs_dir = logs_dir or self._logs_dir()
            path = os.path.join(logs_dir, time.strftime("netlog_%Y%m%d_%H%M%S.csv"))
            f = open(path, "a", newline="", encoding="utf-8")
            csv.writer(f).writerow(["time_iso", "epoch", "gateway",
                                    "lan_ms", "wan_ms", "dns_ms",
                                    "dns_server", "signal_pct",
                                    "extra_ms", "extra_target", "load"])
            f.flush()
            self.log_file = f
            self.log_path = path
            self.log_flag.set()
            return path
        except OSError as e:
            self.state.add_event(f"Logging failed to start: {e}", "bad")
            return None

    def stop_logging(self):
        self.log_flag.clear()
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None

    @staticmethod
    def _fmt(v) -> str:
        return "" if v is None else f"{v:.1f}"

    def _log_row(self, lan_rtt, wan_rtt, extra_rtt=None):
        if not self.log_flag.is_set() or self.log_file is None:
            return
        try:
            st = self.state
            with st.lock:
                load_active = st.load_active
            csv.writer(self.log_file).writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"), f"{time.time():.3f}",
                st.gateway or "", self._fmt(lan_rtt), self._fmt(wan_rtt),
                self._fmt(self._last_dns),
                st.dns_server or "", self._fmt(self._last_signal),
                self._fmt(extra_rtt), st.extra_host or "",
                "1" if load_active else "",
            ])
            self.log_file.flush()
        except Exception:
            pass  # never let logging take down collection


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG      = "#0b0f14"
PANEL   = "#11161d"
CARD    = "#151c26"
BORDER  = "#263041"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"
GREEN   = "#4ade80"
RED     = "#f87171"
YELLOW  = "#fbbf24"
BLUE    = "#60a5fa"
CYAN    = "#22d3ee"
PURPLE  = "#c084fc"
ROSE    = "#fb7185"

STATUS_STYLES = {
    "ONLINE":       (GREEN,  "● ONLINE"),
    "NO INTERNET":  (YELLOW, "● NO INTERNET"),
    "NO ROUTER":    (YELLOW, "● NO ROUTER"),
    "OFFLINE":      (RED,    "● OFFLINE"),
    "STARTING":     (MUTED,  "● STARTING…"),
}


def blend(c1: str, c2: str, t: float) -> str:
    """Blend two hex colors; t=0 -> c1, t=1 -> c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Graph widget (custom Canvas line chart with hover tooltip)
# ---------------------------------------------------------------------------

class GraphWidget(tk.Canvas):
    def __init__(self, parent, title: str, unit: str, color: str,
                 max_points: int = 300, fixed_range: tuple | None = None,
                 bg: str = PANEL, fonts: dict | None = None,
                 empty_text: str = "waiting for data…", **kw):
        super().__init__(parent, bg=bg, highlightthickness=1,
                         highlightbackground=BORDER, **kw)
        self.title, self.unit, self.color = title, unit, color
        self.max_points = max_points
        self.fixed_range = fixed_range
        self.fonts = fonts or {}
        self.empty_text = empty_text
        self.data: list[Sample] = []
        self._hover: list[int] = []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._clear_hover())

    def set_data(self, data: list[Sample], spans: list | None = None):
        self.data = data[-self.max_points:]
        self.spans = spans or []
        self.redraw()

    # ---- drawing ----

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 60:
            return
        f = self.fonts
        pad_l, pad_r, pad_t, pad_b = 46 * (f.get("s", 1)), 12, 30, 20
        self._plot = (pad_l, pad_t, w - pad_r, h - pad_b)

        # frame grid
        for i in range(5):
            y = pad_t + (self._plot[3] - pad_t) * i / 4
            self.create_line(pad_l, y, self._plot[2], y, fill=BORDER)
        self.create_text(pad_l, 14, text=self.title.upper(), anchor="w",
                         fill=MUTED, font=f.get("small"))

        vals = [s.v for s in self.data if s.v is not None]
        if self.fixed_range:
            lo, hi = self.fixed_range
        elif vals:
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.15
            lo, hi = max(0.0, lo - pad), hi + pad
        else:
            lo, hi = 0.0, 10.0
        self._range = (lo, hi)

        def py(v):
            return self._plot[3] - (v - lo) / (hi - lo) * (self._plot[3] - pad_t)

        self._py = py

        # y-axis labels
        for i in range(5):
            v = hi - (hi - lo) * i / 4
            y = pad_t + (self._plot[3] - pad_t) * i / 4
            self.create_text(pad_l - 6, y, text=f"{v:.0f}" if hi - lo >= 4 else f"{v:.1f}",
                             anchor="e", fill=MUTED, font=f.get("tiny"))

        # x-axis labels
        span = len(self.data)
        self.create_text(pad_l, h - 8, text=f"-{span}s" if span < 120 else f"-{span // 60}m",
                         anchor="w", fill=MUTED, font=f.get("tiny"))
        self.create_text(self._plot[2], h - 8, text="now", anchor="e",
                         fill=MUTED, font=f.get("tiny"))

        if not self.data:
            self.create_text(w / 2, (pad_t + self._plot[3]) / 2, text=self.empty_text,
                             fill=MUTED, font=f.get("small"))
            return

        n = len(self.data)
        def px(i):
            return pad_l + i / max(1, n - 1) * (self._plot[2] - pad_l)
        self._px = px

        fill_color = blend(self.color, BG, 0.82)

        # load-test windows: shaded bands so latency-under-load is visible
        for t0, t1, label in getattr(self, "spans", []):
            idx = [i for i, s in enumerate(self.data)
                   if s.t >= t0 and (t1 is None or s.t <= t1)]
            if len(idx) >= 2:
                x0, x1 = px(idx[0]), px(idx[-1])
                self.create_rectangle(x0, pad_t, x1, self._plot[3],
                                      fill=blend(BLUE, BG, 0.86), outline="")
                self.create_text(x0 + 4, pad_t + 8, anchor="w", text=label or "LOAD",
                                 fill=blend(BLUE, TEXT, 0.55), font=f.get("tiny"))

        # contiguous segments (gaps where v is None)
        seg, segments = [], []
        for i, s in enumerate(self.data):
            if s.v is None:
                if seg:
                    segments.append(seg)
                    seg = []
            else:
                seg.append((px(i), py(s.v)))
        if seg:
            segments.append(seg)

        for pts in segments:
            if len(pts) >= 2:
                self.create_polygon(pts[0][0], self._plot[3], *sum(pts, ()),
                                    pts[-1][0], self._plot[3],
                                    fill=fill_color, outline="")
                coords = [c for p in pts for c in p]
                self.create_line(*coords, fill=self.color, width=2, joinstyle="round")
            else:  # single isolated sample — draw a dot
                x, y = pts[0]
                self.create_oval(x - 2, y - 2, x + 2, y + 2,
                                 fill=self.color, outline="")

        # loss markers (red ticks along the bottom)
        for i, s in enumerate(self.data):
            if s.v is None:
                x = px(i)
                self.create_line(x, self._plot[3] - 6, x, self._plot[3] - 1, fill=RED, width=2)

        if not vals:
            self.create_text((pad_l + self._plot[2]) / 2, (pad_t + self._plot[3]) / 2,
                             text="no successful samples", fill=MUTED, font=f.get("small"))

        # average (dashed)
        if vals:
            avg = statistics.fmean(vals)
            self.create_line(pad_l, py(avg), self._plot[2], py(avg),
                             fill=blend(self.color, TEXT, 0.5), dash=(4, 4))
            self.create_text(self._plot[2] - 4, py(avg) - 8, anchor="e",
                             text=f"avg {avg:.1f}", fill=blend(self.color, TEXT, 0.6),
                             font=f.get("tiny"))

        # current value, top-right
        last = next((s.v for s in reversed(self.data) if s.v is not None), None)
        if last is not None:
            self.create_text(self._plot[2], 14, anchor="e",
                             text=f"{last:.1f} {self.unit}", fill=self.color, font=f.get("value"))

    # ---- hover ----

    def _clear_hover(self):
        for i in self._hover:
            self.delete(i)
        self._hover = []

    def _on_motion(self, e):
        self._clear_hover()
        if not self.data or not hasattr(self, "_px") or not hasattr(self, "_py"):
            return
        n = len(self.data)
        i = round((e.x - self._plot[0]) / max(1e-9, self._plot[2] - self._plot[0]) * (n - 1))
        i = max(0, min(n - 1, i))
        s = self.data[i]
        x = self._px(i)
        if s.v is None:
            label = "timeout"
            y = self._plot[3] - 10
        else:
            label = f"{s.v:.1f} {self.unit}"
            y = self._py(s.v)
        self._hover.append(self.create_oval(x - 3, y - 3, x + 3, y + 3,
                                            fill=self.color, outline=TEXT, width=1))
        tx = min(x + 10, self._plot[2] - 110)
        self._hover.append(self.create_rectangle(tx, y - 22, tx + 104, y - 6,
                                                 fill=BG, outline=BORDER))
        self._hover.append(self.create_text(tx + 52, y - 14, text=label,
                                            fill=TEXT, font=self.fonts.get("tiny")))


# ---------------------------------------------------------------------------
# Dashboard GUI
# ---------------------------------------------------------------------------

class Dashboard(tk.Tk):
    def __init__(self, state: State, collector: Collector):
        self._dpi_ready = False
        try:  # must happen before the Tk window is created
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            self._dpi_ready = True
        except Exception:
            pass
        super().__init__()
        self.state, self.collector = state, collector
        self.title("Network Monitor")
        self.configure(bg=BG)
        self.geometry("1280x820")
        self.minsize(1000, 640)

        try:
            self._scale = (ctypes.windll.user32.GetDpiForSystem() / 96.0
                           if self._dpi_ready else 1.0)
        except Exception:
            self._scale = 1.0
        s = self._scale

        self.fonts = {
            "s": s,
            "title":  tkfont.Font(family="Segoe UI", size=-int(13 * s), weight="bold"),
            "card":   tkfont.Font(family="Segoe UI", size=-int(11 * s)),
            "value":  tkfont.Font(family="Consolas", size=-int(12 * s)),
            "big":    tkfont.Font(family="Segoe UI", size=-int(22 * s), weight="bold"),
            "small":  tkfont.Font(family="Segoe UI", size=-int(10 * s)),
            "tiny":   tkfont.Font(family="Segoe UI", size=-int(9 * s)),
            "sub":    tkfont.Font(family="Consolas", size=-int(9 * s)),
        }

        self._build_header()
        self._build_targets()
        self._build_cards()
        self._build_graphs()
        self._build_log()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._refresh)

    # ---- header ----

    def _build_header(self):
        bar = tk.Frame(self, bg=PANEL, pady=10, padx=14)
        bar.grid(row=0, column=0, sticky="ew")
        self.grid_columnconfigure(0, weight=1)

        tk.Label(bar, text="NETWORK MONITOR", bg=PANEL, fg=TEXT,
                 font=self.fonts["title"]).pack(side="left")

        self.status_lbl = tk.Label(bar, text=STATUS_STYLES["STARTING"][1], bg=PANEL,
                                   fg=MUTED, font=self.fonts["title"])
        self.status_lbl.pack(side="left", padx=18)

        self.wifi_lbl = tk.Label(bar, text="", bg=PANEL, fg=MUTED, font=self.fonts["tiny"],
                                 justify="right")
        self.wifi_lbl.pack(side="right")
        self.clock_lbl = tk.Label(bar, text="", bg=PANEL, fg=TEXT, font=self.fonts["small"])
        self.clock_lbl.pack(side="right", padx=(0, 20))

        sep = tk.Frame(self, bg=BORDER, height=1)
        sep.grid(row=1, column=0, sticky="ew")

    # ---- target editor ----

    def _build_targets(self):
        bar = tk.Frame(self, bg=PANEL, pady=6, padx=14)
        bar.grid(row=2, column=0, sticky="ew")

        def mk(label, initial, on_apply):
            tk.Label(bar, text=label, bg=PANEL, fg=MUTED,
                     font=self.fonts["small"]).pack(side="left", padx=(0, 6))
            var = tk.StringVar(value=initial or "")
            ent = tk.Entry(bar, textvariable=var, bg=CARD, fg=TEXT,
                           insertbackground=TEXT, relief="flat",
                           font=self.fonts["sub"], width=20)
            ent.pack(side="left", padx=(0, 18))
            ent.bind("<Return>", lambda e: on_apply())
            return var

        self.wan_var = mk("WAN TARGET", self.collector.wan_host, self._apply_targets)
        self.extra_var = mk("CUSTOM TARGET", self.collector.extra_host, self._apply_targets)
        self.dns_var = mk("DNS TARGET", self.collector.dns_server, self._apply_dns)
        tk.Label(bar, text="IP or hostname · press Enter to apply",
                 bg=PANEL, fg=MUTED, font=self.fonts["tiny"]).pack(side="left")

    def _apply_targets(self):
        if not self.collector.set_targets(self.wan_var.get(),
                                          self.extra_var.get().strip() or None):
            self.state.add_event("WAN target cannot be empty", "warn")

    def _apply_dns(self):
        self.collector.set_dns(self.dns_var.get())

    # ---- stat cards ----

    def _card(self, parent, col, title, color):
        card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, padx=12, pady=8)
        parent.grid_columnconfigure(col, weight=1, uniform="c")
        lbl = tk.Label(card, text=title, bg=CARD, fg=MUTED, font=self.fonts["card"])
        lbl.pack(anchor="w", fill="x")
        val = tk.Label(card, text="—", bg=CARD, fg=color, font=self.fonts["big"])
        val.pack(anchor="w", fill="x")
        sub = tk.Label(card, text="", bg=CARD, fg=MUTED, font=self.fonts["sub"])
        sub.pack(anchor="w", fill="x")
        card.grid(row=0, column=col, sticky="nsew", padx=4, pady=4)
        return val, sub

    def _build_cards(self):
        row = tk.Frame(self, bg=BG)
        row.grid(row=3, column=0, sticky="ew", padx=8, pady=(8, 0))
        self.cards = {}
        for col, (key, title, color) in enumerate([
                ("lan", "LAN LATENCY", CYAN), ("wan", "WAN LATENCY", GREEN),
                ("jitter", "JITTER", YELLOW), ("loss", "PACKET LOSS", RED),
                ("signal", "WIFI SIGNAL", BLUE), ("dns", "DNS LATENCY", PURPLE)]):
            self.cards[key] = self._card(row, col, title, color)
        self.cards["signal"][0].config(fg=BLUE)
        self.cards["loss"][0].config(fg=GREEN)

    # ---- graphs + log ----

    def _build_graphs(self):
        body = tk.Frame(self, bg=BG)
        body.grid(row=4, column=0, sticky="nsew", padx=8, pady=8)
        self.grid_rowconfigure(4, weight=1)
        for c in range(3):
            body.grid_columnconfigure(c, weight=1, uniform="g")
        body.grid_rowconfigure(0, weight=3, uniform="gh")
        body.grid_rowconfigure(1, weight=2, uniform="gh")

        self.g_wan = GraphWidget(body, "Internet latency (WAN)", "ms", GREEN,
                                 fonts=self.fonts, bg=CARD)
        self.g_extra = GraphWidget(body, "Custom target latency", "ms", ROSE,
                                   fonts=self.fonts, bg=CARD,
                                   empty_text="not configured — set Custom target")
        self.g_lan = GraphWidget(body, "Router latency (LAN)", "ms", CYAN,
                                 fonts=self.fonts, bg=CARD)
        self.g_wan.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.g_extra.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        self.g_lan.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)

        self.g_jitter = GraphWidget(body, "Jitter", "ms", YELLOW, fonts=self.fonts, bg=CARD)
        self.g_signal = GraphWidget(body, "WiFi signal", "%", BLUE,
                                    fixed_range=(0, 100), fonts=self.fonts, bg=CARD)
        self.g_dns = GraphWidget(body, "DNS latency", "ms", PURPLE, fonts=self.fonts, bg=CARD)
        self.g_jitter.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.g_signal.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self.g_dns.grid(row=1, column=2, sticky="nsew", padx=4, pady=4)

    def _build_log(self):
        bar = tk.Frame(self, bg=PANEL)
        bar.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 8))
        tk.Label(bar, text="EVENT LOG", bg=PANEL, fg=MUTED,
                 font=self.fonts["card"]).pack(side="left")
        self.pause_btn = tk.Button(bar, text="Pause", command=self._toggle_pause, bg=CARD,
                                   fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
                                   relief="flat", font=self.fonts["small"], padx=12)
        self.pause_btn.pack(side="right", padx=4)
        tk.Button(bar, text="Load test", command=self._open_load_dialog, bg=CARD,
                  fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", font=self.fonts["small"], padx=12).pack(side="right")
        tk.Button(bar, text="Reset stats", command=self._reset_stats, bg=CARD, fg=TEXT,
                  activebackground=BORDER, activeforeground=TEXT, relief="flat",
                  font=self.fonts["small"], padx=12).pack(side="right")
        self._paused = False
        self.log_btn = tk.Button(bar, text="Logging: OFF", command=self._toggle_logging,
                                 bg=CARD, fg=MUTED, activebackground=BORDER,
                                 activeforeground=TEXT, relief="flat",
                                 font=self.fonts["small"], padx=12)
        self.log_btn.pack(side="right", padx=(0, 12))

        self.log = tk.Text(self, bg=PANEL, fg=TEXT, height=7, relief="flat",
                           font=self.fonts["sub"], state="disabled", padx=10, pady=6)
        self.log.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 10))
        for tag, color in (("good", GREEN), ("bad", RED), ("warn", YELLOW), ("info", MUTED)):
            self.log.tag_configure(tag, foreground=color)

    # ---- actions ----

    def _toggle_logging(self):
        if not self.collector.log_flag.is_set():
            path = self.collector.start_logging()
            if path:
                self.log_btn.config(text="Logging: ON", fg=GREEN)
                self.state.add_event(f"Logging to {path}", "good")
        else:
            self.collector.stop_logging()
            self.log_btn.config(text="Logging: OFF", fg=MUTED)
            self.state.add_event("Logging stopped", "warn")

    def _toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self.collector.stop_flag.set()
            self.pause_btn.config(text="Resume")
            self.state.add_event("Monitoring paused", "warn")
        else:
            self.collector.stop_flag.clear()
            threading.Thread(target=self.collector.run, daemon=True).start()
            self.pause_btn.config(text="Pause")
            self.state.add_event("Monitoring resumed", "good")

    def _reset_stats(self):
        with self.state.lock:
            self.state.counts = {"wan_sent": 0, "wan_recv": 0, "lan_sent": 0,
                                 "lan_recv": 0, "extra_sent": 0, "extra_recv": 0}
            self.state.start_time = time.time()
        self.state.add_event("Statistics reset")

    # ---- load test ----

    def _open_load_dialog(self):
        f = self.fonts
        dlg = tk.Toplevel(self)
        dlg.title("Load test")
        dlg.configure(bg=PANEL, padx=14, pady=10)
        dlg.transient(self)
        dlg.grab_set()

        mode = tk.StringVar(value="tcp")
        target = tk.StringVar(value="wan")
        direction = tk.StringVar(value="both")
        seconds = tk.StringVar(value="15")
        streams = tk.StringVar(value="4")
        mbps = tk.StringVar(value="0")
        udp_host = tk.StringVar(value="")
        udp_port = tk.StringVar(value="9")

        def lab(parent, text):
            tk.Label(parent, text=text, bg=PANEL, fg=MUTED,
                     font=f["small"]).pack(side="left")

        def ent(parent, var, w=8):
            tk.Entry(parent, textvariable=var, bg=CARD, fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=f["sub"], width=w).pack(side="left", padx=6)

        def radio(parent, var, val, text):
            tk.Radiobutton(parent, text=text, variable=var, value=val, bg=PANEL, fg=TEXT,
                           selectcolor=CARD, activebackground=PANEL, font=f["small"],
                           activeforeground=TEXT, relief="flat").pack(side="left")

        r1 = tk.Frame(dlg, bg=PANEL); r1.pack(anchor="w", pady=2)
        lab(r1, "MODE   ")
        radio(r1, mode, "tcp", "TCP peer (mini-iperf)    ")
        radio(r1, mode, "udp", "Rude UDP blast")
        r2 = tk.Frame(dlg, bg=PANEL); r2.pack(anchor="w", pady=2)
        lab(r2, "TARGET   ")
        radio(r2, target, "gateway", "Gateway    ")
        radio(r2, target, "wan", "WAN    ")
        radio(r2, target, "custom", "Custom")
        r3 = tk.Frame(dlg, bg=PANEL); r3.pack(anchor="w", pady=2)
        lab(r3, "UDP TARGET   ")
        ent(r3, udp_host, 16)
        lab(r3, "  port ")
        ent(r3, udp_port, 6)
        r4 = tk.Frame(dlg, bg=PANEL); r4.pack(anchor="w", pady=2)
        lab(r4, "DIRECTION   ")
        radio(r4, direction, "down", "Download    ")
        radio(r4, direction, "up", "Upload    ")
        radio(r4, direction, "both", "Both")
        r5 = tk.Frame(dlg, bg=PANEL); r5.pack(anchor="w", pady=2)
        lab(r5, "SECONDS ")
        ent(r5, seconds)
        lab(r5, "   STREAMS ")
        ent(r5, streams)
        lab(r5, "   RATE CAP Mbps (0 = flat out) ")
        ent(r5, mbps, 10)

        # center the dialog on the app window (needs final size first)
        dlg.update_idletasks()
        dw, dh = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{self.winfo_x() + (self.winfo_width() - dw) // 2}"
                     f"+{self.winfo_y() + (self.winfo_height() - dh) // 2}")

        btns = tk.Frame(dlg, bg=PANEL)
        btns.pack(anchor="e", pady=(10, 0))

        def run():
            try:
                params = {
                    "mode": mode.get(), "target": target.get(), "dir": direction.get(),
                    "seconds": min(MAX_LOAD_SECONDS, max(1.0, float(seconds.get()))),
                    "streams": max(1, min(64, int(streams.get()))),
                    "mbps": max(0.0, float(mbps.get())),
                    "udp_host": udp_host.get().strip(),
                    "udp_port": max(1, min(65535, int(udp_port.get()))),
                }
            except ValueError:
                self.state.add_event("Load test: invalid parameters", "warn")
                return
            dlg.destroy()
            threading.Thread(target=self._run_load_test, args=(params,),
                             daemon=True).start()

        tk.Button(btns, text="Run", command=run, bg=CARD, fg=GREEN,
                  activebackground=BORDER, activeforeground=TEXT, relief="flat",
                  font=f["small"], padx=14).pack(side="left", padx=4)
        tk.Button(btns, text="Cancel", command=dlg.destroy, bg=CARD, fg=TEXT,
                  activebackground=BORDER, activeforeground=TEXT, relief="flat",
                  font=f["small"], padx=14).pack(side="left")

    def _run_load_test(self, p: dict):
        st = self.state
        col = self.collector
        if p["mode"] == "udp":
            if not p["udp_host"]:
                st.add_event("Load test: UDP target host required", "warn")
                return
            host, label = p["udp_host"], f"UDP {p['udp_host']}:{p['udp_port']}"
        else:
            if p["target"] == "gateway":
                host, label = st.gateway, "gateway"
            elif p["target"] == "wan":
                host, label = col._wan_ip or col.wan_host, f"WAN {col.wan_host}"
            elif col.extra_host:
                host, label = col.extra_host, f"custom {col.extra_host}"
            else:
                host, label = None, None
            if not host:
                st.add_event("Load test: target unavailable (set it in the top bar)", "warn")
                return
        t0 = time.time()
        with st.lock:
            st.load_active = True
            st.load_spans.append([t0, None, "LOAD"])
        st.add_event(f"Load test started — {label}, {p['seconds']:.0f}s", "warn")
        try:
            if p["mode"] == "udp":
                got = udp_blast(host, p["udp_port"], p["seconds"], p["mbps"] or 20.0)
                res_txt = f"rude UDP → {label}: {got:.0f} Mbps" if got else "rude UDP failed"
            else:
                res = tcp_load_client(host, seconds=p["seconds"], streams=p["streams"],
                                      direction=p["dir"], mbps=p["mbps"])
                parts = [f"{d} {res[d]:.0f} Mbps" for d in ("down", "up")
                         if res.get(d) is not None]
                res_txt = (f"TCP peer {label}: " + ", ".join(parts)) if parts \
                    else "TCP peer: no data"
        finally:
            t1 = time.time()
            with st.lock:
                st.load_active = False
                for sp in st.load_spans:
                    if sp[1] is None:
                        sp[1] = t1
        st.add_event(res_txt, "good")
        # bufferbloat: idle-vs-loaded mean latency on every probe path
        with st.lock:
            rows = []
            for name, deq in (("LAN", st.lan), ("WAN", st.wan), ("DNS", st.dns),
                              ("CUSTOM", st.extra)):
                bb = bufferbloat(list(deq), t0, t1)
                if bb and bb[1] is not None:
                    rows.append(f"{name} {bb[0]:.0f}→{bb[1]:.0f} ms" if bb[0] is not None
                                else f"{name} →{bb[1]:.0f} ms (no idle baseline)")
        if rows:
            st.add_event("Latency under load (idle→loaded): " + "  ·  ".join(rows), "warn")

    # ---- refresh loop ----

    def _refresh(self):
        try:
            self._update()
        finally:
            self.after(1000, self._refresh)

    def _update(self):
        st = self.state
        self.clock_lbl.config(text=time.strftime("%H:%M:%S"))

        with st.lock:
            lan, wan, dns, sig, extra = (list(st.lan), list(st.wan), list(st.dns),
                                         list(st.signal), list(st.extra))
            spans = list(st.load_spans)
            counts = dict(st.counts)
            wifi, wifi_avail = st.wifi, st.wifi_available
            events = list(st.events)

        # --- stats ---
        def present(series):
            return [s.v for s in series if s.v is not None]

        lan_v, wan_v, dns_v = present(lan), present(wan), present(dns)

        jitter = None
        wan_present = [s.v for s in wan if s.v is not None]
        if len(wan_present) >= 2:
            jitter = statistics.fmean(
                [abs(b - a) for a, b in zip(wan_present, wan_present[1:])])

        loss_pct = 100.0 * (counts["wan_sent"] - counts["wan_recv"]) / counts["wan_sent"] \
            if counts["wan_sent"] else 0.0

        def set_card(key, val, sub, color=None):
            v, s = self.cards[key]
            v.config(text=val)
            s.config(text=sub)
            if color:
                v.config(fg=color)

        set_card("lan", f"{lan_v[-1]:.1f} ms" if lan_v else "—",
                 f"min {min(lan_v):.0f} · max {max(lan_v):.0f} ms" if lan_v else "no reply",
                 color=None if lan_v else RED)
        set_card("wan", f"{wan_v[-1]:.1f} ms" if wan_v else "—",
                 f"min {min(wan_v):.0f} · max {max(wan_v):.0f} ms" if wan_v else "no reply")
        set_card("jitter", f"{jitter:.1f} ms" if jitter is not None else "—",
                 "mean |Δ| consecutive")
        loss_color = GREEN if loss_pct < 2 else (YELLOW if loss_pct < 10 else RED)
        set_card("loss", f"{loss_pct:.1f} %",
                 f"{counts['wan_sent'] - counts['wan_recv']} of {counts['wan_sent']} lost",
                 color=loss_color)

        if sig and sig[-1].v is not None:
            extra = (wifi.get("channel") or wifi.get("band") or "") if wifi else ""
            set_card("signal", f"{sig[-1].v:.0f} %", extra or "signal quality", color=BLUE)
        else:
            set_card("signal", "—",
                     "needs admin rights" if wifi_avail is False and IS_WINDOWS
                     else ("unavailable on this platform" if wifi_avail is False else "no data"))
        self.g_signal.empty_text = (
            "run as Administrator for WiFi data" if (wifi_avail is False and IS_WINDOWS)
            else ("not available on this platform" if wifi_avail is False
                  else "waiting for data…"))

        shown_dns = st.dns_server if self.collector.dns_server == "auto" else self.collector.dns_server
        set_card("dns", f"{dns_v[-1]:.1f} ms" if dns_v else "—",
                 f"server {shown_dns or '…'}")

        # --- status pill ---
        lan_ok = bool(lan) and lan[-1].v is not None
        wan_ok = bool(wan) and wan[-1].v is not None
        if not wan and not lan:
            key = "STARTING"
        elif lan_ok and wan_ok:
            key = "ONLINE"
        elif lan_ok:
            key = "NO INTERNET"
        elif wan_ok:
            key = "NO ROUTER"  # internet reachable, gateway not (e.g. VPN)
        else:
            key = "OFFLINE"
        color, label = STATUS_STYLES[key]
        self.status_lbl.config(text=label, fg=color)

        # --- wifi header ---
        if wifi:
            parts = []
            if "ssid" in wifi:
                parts.append(wifi["ssid"])
            if "signal" in wifi:
                parts.append(f"{wifi['signal']}%")
            if "band" in wifi:
                parts.append(wifi["band"])
            if "rx" in wifi:
                parts.append(wifi["rx"])
            self.wifi_lbl.config(text="  ·  ".join(parts))
        else:
            self.wifi_lbl.config(text="WiFi details unavailable" if wifi_avail else "")

        # --- graphs ---
        window = st.window
        self.g_wan.set_data(wan[-window:], spans)
        self.g_lan.set_data(lan[-window:], spans)
        self.g_dns.set_data(dns[-window:], spans)
        self.g_extra.set_data(extra[-window:], spans)
        # jitter series: |Δ| between consecutive present samples
        js = []
        prev_t = prev_v = None
        for s in wan:
            if s.v is None:
                js.append(Sample(s.t, None))
                prev_t = prev_v = None
            elif prev_v is not None:
                js.append(Sample(s.t, abs(s.v - prev_v)))
                prev_t, prev_v = s.t, s.v
            else:
                js.append(Sample(s.t, None))
                prev_t, prev_v = s.t, s.v
        self.g_jitter.set_data(js[-window:], spans)
        self.g_signal.set_data(sig[-window:])

        # --- event log ---
        if len(events) != getattr(self, "_log_len", -1):
            self._log_len = len(events)
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            for e in reversed(list(events)[-50:]):
                msg, kind = e.v
                self.log.insert("end", f"{e.t}  ", "info")
                self.log.insert("end", msg + "\n", kind)
            self.log.config(state="disabled")

    def _on_close(self):
        self.collector.stop_flag.set()
        self.collector.stop_logging()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def headless(state: State, collector: Collector, seconds: float = 5.0):
    collector.start()
    time.sleep(seconds)
    series = [("LAN", state.lan), ("WAN", state.wan), ("DNS", state.dns)]
    if state.extra_host:
        series.insert(2, ("CUSTOM", state.extra))
    with state.lock:
        for name, deq in series:
            vals = [s.v for s in deq if s.v is not None]
            print(f"{name}: {len(deq)} samples, {len(vals)} ok"
                  + (f", avg {statistics.fmean(vals):.1f} ms" if vals else ""))
        print("gateway:", state.gateway, "| wifi:", state.wifi)
        for e in state.events:
            print(f"  [{e.t}] {e.v[0]}")


def main():
    ap = argparse.ArgumentParser(description="WiFi / Network Monitor")
    ap.add_argument("--interval", type=float, default=1.0, help="probe interval seconds")
    ap.add_argument("--window", type=int, default=300, help="history window (samples)")
    ap.add_argument("--wan", default="1.1.1.1", help="WAN probe host (IP or hostname)")
    ap.add_argument("--extra", default=None,
                    help="optional custom probe target (IP or hostname, "
                         "e.g. an internal routed hop)")
    ap.add_argument("--dns-server", default="auto",
                    help="DNS probe server ('auto' = use system DNS)")
    ap.add_argument("--dns-name", default="www.google.com", help="DNS probe name")
    ap.add_argument("--no-gui", action="store_true", help="headless diagnostic mode")
    ap.add_argument("--serve", action="store_true",
                    help="act as a load-test peer (mini-iperf server); Ctrl+C to stop")
    ap.add_argument("--port", type=int, default=DEFAULT_LOAD_PORT,
                    help="load-test server port (default 5215)")
    ap.add_argument("--load-test", metavar="HOST",
                    help="run a load test against HOST (needs a --serve peer, "
                         "or --rude) then exit")
    ap.add_argument("--load-dir", choices=("down", "up", "both"), default="both",
                    help="load test direction (default both)")
    ap.add_argument("--load-seconds", type=float, default=15.0,
                    help="load test duration (default 15)")
    ap.add_argument("--load-streams", type=int, default=4,
                    help="parallel streams (default 4)")
    ap.add_argument("--load-mbps", type=float, default=0.0,
                    help="rate cap in Mbps (0 = flat out)")
    ap.add_argument("--rude", action="store_true",
                    help="with --load-test: rude UDP blast (no peer needed)")
    ap.add_argument("--rude-port", type=int, default=9,
                    help="rude mode target port (default 9 = discard)")
    args = ap.parse_args()

    if args.serve:
        load_server(args.port)
        return
    if args.load_test:
        cli_load_test(args.load_test, seconds=args.load_seconds,
                      streams=args.load_streams, direction=args.load_dir,
                      mbps=args.load_mbps, rude=args.rude, rude_port=args.rude_port)
        return

    state = State(args.window)
    collector = Collector(state, args.interval, args.wan, args.dns_server,
                          args.dns_name, args.extra)

    if args.no_gui:
        headless(state, collector)
        return

    collector.start()
    Dashboard(state, collector).mainloop()


if __name__ == "__main__":
    main()
