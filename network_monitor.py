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


# ---- Raw UDP DNS probe (same wire-craft as our DNS benchmark) --------------

def _build_query(name: str) -> tuple[int, bytes]:
    qid = int.from_bytes(socket.os.urandom(2), "big")
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(l)]) + l.encode("ascii") for l in name.split(".") if l) + b"\x00"
    return qid, header + qname + struct.pack(">HH", 1, 1)


def dns_probe(server: str = "1.1.1.1", name: str = "www.google.com", timeout: float = 2.0) -> float | None:
    """Time a real A-record query over UDP. Returns RTT in ms or None."""
    qid, pkt = _build_query(name)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(1024)
        s.close()
        if len(data) < 12 or data[:2] != struct.pack(">H", qid):
            return None
        return (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None


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
            out = subprocess.run(["resolvectl", "status"], capture_output=True, text=True).stdout
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
                                 capture_output=True, text=True).stdout
            m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
            return m.group(1) if m else None
        except Exception:
            return None
    try:
        out = subprocess.run(["route", "print", "-4", "0.0.0.0"],
                             capture_output=True, text=True, timeout=10).stdout
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
                             capture_output=True, text=True, timeout=8).stdout
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
        self.dns = deque(maxlen=window)
        self.signal = deque(maxlen=window)
        self.events = deque(maxlen=100)
        self.wifi = None
        self.gateway = None
        self.wifi_available = None  # None = unknown, False = unavailable
        self.dns_server = None
        self.start_time = time.time()
        self.counts = {"wan_sent": 0, "wan_recv": 0, "lan_sent": 0, "lan_recv": 0}
        self._lan_ok = self._wan_ok = None
        self._signal_warned = False

    def add_event(self, msg: str, kind: str = "info"):
        self.events.append(Sample(time.strftime("%H:%M:%S"), (msg, kind)))

    def record(self, deq, v):
        with self.lock:
            deq.append(Sample(time.time(), v))

    def check_transitions(self, lan_ok: bool, wan_ok: bool):
        if self._lan_ok is not None:
            if self._lan_ok and not lan_ok:
                self.add_event("Connection to router LOST", "bad")
            elif not self._lan_ok and lan_ok:
                self.add_event("Router connection restored", "good")
            if self._wan_ok and not wan_ok:
                self.add_event("Internet connectivity LOST", "bad")
            elif not self._wan_ok and wan_ok and not self._lan_ok:
                pass
            elif not self._wan_ok and wan_ok:
                self.add_event("Internet connectivity restored", "good")
        self._lan_ok, self._wan_ok = lan_ok, wan_ok


class Collector(threading.Thread):
    def __init__(self, state: State, interval: float, wan_host: str, dns_server: str, dns_name: str):
        super().__init__(daemon=True)
        self.state = state
        self.interval = interval
        self.wan_host = wan_host
        self.dns_server = dns_server
        self.dns_name = dns_name
        self.stop_flag = threading.Event()
        self._tick = 0
        self._began = False
        # CSV logging
        self.log_flag = threading.Event()
        self.log_file = None
        self.log_path = None
        self._last_dns = None
        self._last_signal = None

    def run(self):
        st = self.state
        if not self._began:
            self._began = True
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

            lan_rtt = icmp_ping(st.gateway) if st.gateway else None
            wan_rtt = icmp_ping(self.wan_host)
            with st.lock:
                st.counts["lan_sent"] += 1
                st.counts["wan_sent"] += 1
                st.counts["lan_recv"] += lan_rtt is not None
                st.counts["wan_recv"] += wan_rtt is not None
            st.record(st.lan, lan_rtt)
            st.record(st.wan, wan_rtt)
            st.check_transitions(lan_rtt is not None, wan_rtt is not None)

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
                    st.add_event("WiFi details unavailable — restart as Administrator "
                                 "to enable signal/SSID (use run_admin.bat)", "warn")
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
            self._log_row(lan_rtt, wan_rtt)
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
                                    "dns_server", "signal_pct"])
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

    def _log_row(self, lan_rtt, wan_rtt):
        if not self.log_flag.is_set() or self.log_file is None:
            return
        try:
            st = self.state
            csv.writer(self.log_file).writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"), f"{time.time():.3f}",
                st.gateway or "", self._fmt(lan_rtt), self._fmt(wan_rtt),
                self._fmt(self._last_dns),
                st.dns_server or "", self._fmt(self._last_signal),
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

    def set_data(self, data: list[Sample]):
        self.data = data[-self.max_points:]
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

    # ---- stat cards ----

    def _card(self, parent, col, title, color):
        card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, padx=12, pady=8)
        parent.grid_columnconfigure(col, weight=1)
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
        row.grid(row=2, column=0, sticky="ew", padx=8, pady=(8, 0))
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
        body.grid(row=3, column=0, sticky="nsew", padx=8, pady=8)
        self.grid_rowconfigure(3, weight=1)
        for c in range(3):
            body.grid_columnconfigure(c, weight=1, uniform="g")
        body.grid_rowconfigure(0, weight=3, uniform="gh")
        body.grid_rowconfigure(1, weight=2, uniform="gh")

        self.g_wan = GraphWidget(body, "Internet latency (WAN)", "ms", GREEN,
                                 fonts=self.fonts, bg=CARD)
        self.g_lan = GraphWidget(body, "Router latency (LAN)", "ms", CYAN,
                                 fonts=self.fonts, bg=CARD)
        self.g_wan.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
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
        bar.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
        tk.Label(bar, text="EVENT LOG", bg=PANEL, fg=MUTED,
                 font=self.fonts["card"]).pack(side="left")
        self.pause_btn = tk.Button(bar, text="Pause", command=self._toggle_pause, bg=CARD,
                                   fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
                                   relief="flat", font=self.fonts["small"], padx=12)
        self.pause_btn.pack(side="right", padx=4)
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
        self.log.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 10))
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
            self.state.counts = {"wan_sent": 0, "wan_recv": 0, "lan_sent": 0, "lan_recv": 0}
            self.state.start_time = time.time()
        self.state.add_event("Statistics reset")

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
            lan, wan, dns, sig = (list(st.lan), list(st.wan), list(st.dns), list(st.signal))
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
            set_card("signal", "—", "needs admin rights" if wifi_avail is False else "no data")
        self.g_signal.empty_text = ("run as Administrator for WiFi data"
                                    if wifi_avail is False else "waiting for data…")

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
        self.g_wan.set_data(wan[-window:])
        self.g_lan.set_data(lan[-window:])
        self.g_dns.set_data(dns[-window:])
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
        self.g_jitter.set_data(js[-window:])
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
    with state.lock:
        for name, deq in (("LAN", state.lan), ("WAN", state.wan), ("DNS", state.dns)):
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
    ap.add_argument("--wan", default="1.1.1.1", help="WAN probe host (IP)")
    ap.add_argument("--dns-server", default="auto",
                    help="DNS probe server ('auto' = use system DNS)")
    ap.add_argument("--dns-name", default="www.google.com", help="DNS probe name")
    ap.add_argument("--no-gui", action="store_true", help="headless diagnostic mode")
    args = ap.parse_args()

    state = State(args.window)
    collector = Collector(state, args.interval, args.wan, args.dns_server, args.dns_name)

    if args.no_gui:
        headless(state, collector)
        return

    collector.start()
    Dashboard(state, collector).mainloop()


if __name__ == "__main__":
    main()
