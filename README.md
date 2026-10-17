# WiFi / Network Monitor

A professional, dark-themed live network dashboard with **zero third-party
dependencies** — pure Python standard library (`tkinter`, `ctypes`, raw sockets).

![preview](preview.png)

## What it shows

| Panel | Meaning |
|---|---|
| **LAN latency** | ICMP ping to your default gateway — router/WiFi health |
| **WAN latency** | ICMP ping to an internet host (default `1.1.1.1`) |
| **Jitter** | Mean absolute delta of consecutive WAN samples |
| **Packet loss** | Failed WAN probes (per-session %) |
| **WiFi signal** | SSID, signal %, channel (needs elevation on Win 11) |
| **DNS latency** | Real raw-UDP DNS query RTT to your system DNS server |

The **LAN vs WAN split tells you where the problem is**:

- `● ONLINE` — router and internet both reachable
- `● NO INTERNET` — router fine, internet down (ISP/VPN issue)
- `● NO ROUTER` — internet reachable but gateway not answering (e.g. VPN split routing)
- `● OFFLINE` — neither reachable

Graphs feature hover tooltips, loss tick-marks (red), dashed average lines,
and automatic y-scaling. An event log records connectivity transitions,
gateway changes, and weak-signal warnings.

## Run

```powershell
python network_monitor.py
```

Options:

```
--interval 2          probe every N seconds (default 1)
--window 600          history length in samples (default 300)
--wan 9.9.9.9         WAN probe host
--dns-server auto     DNS probe server ('auto' = system DNS, the default)
--no-gui              headless diagnostic mode (prints 5 s of stats)
```

## Logging to file

Click **Logging: OFF/ON** in the bottom bar. One CSV row is appended per
probe interval to a timestamped file — always written **next to the script**
(`wifi-network-monitor/logs/netlog_YYYYMMDD_HHMMSS.csv`, independent of the
launch directory; falls back to `~/NetworkMonitorLogs` if that's unwritable):

```csv
time_iso,epoch,gateway,lan_ms,wan_ms,dns_ms,dns_server,signal_pct
2026-10-16 23:06:01,1792206361.986,10.0.0.1,,60.0,28.6,192.168.0.249,
```

A blank cell means the probe genuinely failed that tick (DNS is probed every
tick, so blanks are real failures — good for outage analysis). WiFi signal is
a sampled state, so it is forward-filled.

## Analyzing logs later

```powershell
python analyze_log.py                     # auto-finds logs (script dir + CWD + ~)
python analyze_log.py logs/netlog_*.csv   # specific files
python analyze_log.py --plot report.png   # + charts (needs matplotlib)
```

Prints per-series min/avg/p50/p95/max, loss %, uptime %, and jitter; with
`--plot` it renders a dark-themed multi-panel chart to PNG.

## Implementation notes

- **ICMP** uses `IcmpSendEcho` from `iphlpapi.dll` via ctypes — no `ping.exe`
  parsing, locale-independent, works without admin rights.
- **DNS probe** builds raw UDP DNS packets by hand (same wire-craft as our
  `dns-benchmark-python` project) and times a real A-record query.
- **Gateway detection** parses `route print -4 0.0.0.0` (IP literals only, so
  it survives non-English Windows). The lowest-metric IPv4 gateway wins.
- **System DNS** is read from `GetNetworkParams` (linked-list walk, no WMI).
- **WiFi details** come from the native WLAN API (`wlanapi.dll` via ctypes,
  locale-free, no admin needed) — SSID, signal %, band, link rates, parsed from
  `WlanQueryInterface` with a layout verified against `netsh` ground truth.
  A `netsh` parser is the fallback; if both fail (e.g. blocked by privacy
  settings) the app degrades gracefully, and `run_admin.bat` relaunches elevated.
- Collector runs on a background thread; the Tk GUI refreshes at 1 Hz and
  redraws only what changed.
