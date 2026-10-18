# Load Testing Guide

The monitor can now *stress* a path, not just watch it. Two modes:

| Mode | Needs a peer? | What it's for |
|------|--------------|---------------|
| **TCP peer** (mini-iperf) | Yes — a second machine running `--serve` | Clean two-way throughput + latency-under-load on an exact path (LAN, VPN, LTE) |
| **Rude UDP blast** | No | Fill an *outbound* path when you can't deploy anything — rate-capped junk UDP |

In both cases the monitor keeps probing as usual, so the latency graphs show
what the load *did*. The load window is shaded on the graphs, the event log
gets an idle→loaded latency summary (bufferbloat), and CSV rows are tagged
in a `load` column for `analyze_log.py`.

---

## 1. Verify it works (one machine, no hardware)

```
:: terminal 1 — the peer
py network_monitor.py --serve

:: terminal 2 — the client
py network_monitor.py --load-test 127.0.0.1 --load-dir both --load-seconds 15
py network_monitor.py --load-test 127.0.0.1 --rude --load-seconds 10 --load-mbps 50
```

Expected: multi-Gbps on loopback, `latency idle→loaded: 0.0 → 0.0 ms`.
Rate caps are honored exactly (a 50 Mbps cap reports ~50.0 Mbps).

## 2. Deploy the far end (the mini-PC)

Almost anything works — for LTE-range testing (<100 Mbps) even a Raspberry Pi
is plenty; for gigabit LAN use an Intel N100 mini-PC or better. Requirements:
Python 3.10+, one Ethernet port, and this file copied over.

**Linux (recommended for the mini-PC)**

```bash
# copy network_monitor.py over, then:
sudo ufw allow 5215/tcp                      # open the port
sudo tee /etc/systemd/system/nm-load.service <<'EOF'
[Unit]
Description=Network Monitor load-test peer
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/nm/network_monitor.py --serve
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now nm-load
```

**Windows (old laptop as the peer)**

```powershell
netsh advfirewall firewall add rule name="nm-load" dir=in action=allow ^
  protocol=TCP localport=5215

:: auto-start at boot, hidden:
schtasks /create /tn "nm-load" /tr "pythonw C:\nm\network_monitor.py --serve" /sc onstart /ru SYSTEM
```

Give it a static DHCP reservation (or note its IP) — the client needs a
stable address.

## 3. Point tests at the paths you care about

The peer's *location* decides what you're measuring:

| Peer location | Tests |
|---|---|
| Same LAN as the laptop | LAN throughput + LAN bufferbloat (switch/cabling) |
| Office/DC, reached through the VPN | **The whole road path**: LTE up/down + VPN. This is the money shot — run `--load-dir both` and watch the WAN graph bloat |
| Another remote network | Baseline for "is it the LTE or the destination?" |

**Download (office → vehicle):** `py network_monitor.py --load-test <peer> --load-dir down`
**Upload (vehicle → office):** `--load-dir up` — expect LTE uplink to be the ugly one.
**Bufferbloat** is printed automatically (the built-in pinger samples the target
1×/s, idle 5 s → load → report) and appears in the GUI event log when run from there.

**No peer available?** Rude mode fills the outbound path toward any target:
`py network_monitor.py --load-test <ip> --rude --load-mbps 20`

### GUI

Load test button (bottom bar, next to Reset stats): pick TCP peer vs rude
UDP, the target path (Gateway / WAN / Custom), direction, duration, stream
count, and an optional rate cap. Results land in the event log; the load
window is shaded on every latency graph.

## 4. Reading the results

- **Throughput** — the headline number. LTE: ~5–15 Mbps up is normal.
- **Bufferbloat (+ms)** — the interesting number:

| idle→loaded | Verdict |
|---|---|
| +0–20 ms | Healthy link / good AQM |
| +20–100 ms | Noticeable — video calls will feel it under load |
| +100–500 ms | Bad — uploads stall everything (classic LTE uplink) |
| +500 ms or loss spikes | Severe bufferbloat / congestion |

- **Where it bloats matters**: if the load ran toward the VPN peer and *WAN*
  (to the concentrator's public IP) bloated too, it's the LTE leg. If only
  the VPN-side numbers degraded, it's the tunnel/company side.
- **CSV**: rows during a load test carry `load=1`; `python analyze_log.py`
  prints idle-vs-loaded averages per series.

## 5. Etiquette & safety rails

- Duration is hard-capped at 300 s, streams at 64, and everything is
  opt-in per run — the app never loads a link in the background.
- Rude mode: keep the rate cap honest, and aim it at a discard/firewalled
  port so the target doesn't answer with ICMP unreachable storms.
- LTE data caps are real: a 15 s full-rate test ≈ 30 MB. Budget accordingly.
- Only load-test networks you own or have permission to test.
