#!/usr/bin/env python3
"""
Analyze CSV logs written by network_monitor.py.

Usage:
  python analyze_log.py                       # summarize all logs in ./logs
  python analyze_log.py logs/netlog_*.csv     # summarize specific files
  python analyze_log.py --plot out.png        # also render graphs (needs matplotlib)

Prints per-series stats (min/avg/p50/p95/max, loss) and a timeline summary.
With --plot, renders latency/jitter/availability charts to a PNG.
"""

import csv
import glob
import os
import statistics
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def default_log_globs() -> list[str]:
    """Look next to this script (where the app writes) and in the CWD."""
    return [
        os.path.join(SCRIPT_DIR, "logs", "netlog_*.csv"),
        os.path.join("logs", "netlog_*.csv"),
        os.path.join(os.path.expanduser("~"), "NetworkMonitorLogs", "netlog_*.csv"),
    ]


def find_logs() -> list[str]:
    for pattern in default_log_globs():
        files = sorted(glob.glob(pattern))
        if files:
            return files
    return []


def load(paths):
    rows = []
    for p in paths:
        try:
            with open(p, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        rows.append({
                            "t": float(r["epoch"]),
                            "iso": r["time_iso"],
                            "gateway": r.get("gateway", ""),
                            "lan": float(r["lan_ms"]) if r["lan_ms"] else None,
                            "wan": float(r["wan_ms"]) if r["wan_ms"] else None,
                            "dns": float(r["dns_ms"]) if r["dns_ms"] else None,
                            "dns_server": r.get("dns_server", ""),
                            "signal": int(float(r["signal_pct"])) if r.get("signal_pct") else None,
                            "extra": float(r["extra_ms"]) if r.get("extra_ms") else None,
                        })
                    except (KeyError, ValueError):
                        continue  # tolerate truncated/partial lines
        except OSError as e:
            print(f"warning: cannot read {p}: {e}")
    rows.sort(key=lambda r: r["t"])
    return rows


def stats(rows, key):
    vals = [r[key] for r in rows if r[key] is not None]
    if not vals:
        return None
    s = sorted(vals)
    p = lambda q: s[min(len(s) - 1, int(len(s) * q))]
    return {"n": len(vals), "loss": 100.0 * (1 - len(vals) / max(1, len(rows))),
            "min": min(vals), "avg": statistics.fmean(vals),
            "p50": p(.5), "p95": p(.95), "max": max(vals)}


def jitter(rows):
    wan = [r["wan"] for r in rows if r["wan"] is not None]
    if len(wan) < 2:
        return None
    return statistics.fmean([abs(b - a) for a, b in zip(wan, wan[1:])])


def report(rows):
    if not rows:
        print("No samples found.")
        return
    dur = rows[-1]["t"] - rows[0]["t"]
    h, rem = divmod(dur, 3600)
    print(f"Samples : {len(rows)} over {int(h)}h {rem/60:.0f}m "
          f"({rows[0]['iso']} -> {rows[-1]['iso']})")
    online = sum(1 for r in rows if r["wan"] is not None)
    routed = sum(1 for r in rows if r["lan"] is not None)
    print(f"Uptime  : internet {100*online/len(rows):.1f}%  |  router {100*routed/len(rows):.1f}%")
    series_list = [("LAN", "lan"), ("WAN", "wan"), ("DNS", "dns")]
    if any(r.get("extra") is not None for r in rows):
        series_list.append(("CUSTOM", "extra"))
    for label, key in series_list:
        s = stats(rows, key)
        if s:
            print(f"{label:4}    : n={s['n']:<5} loss={s['loss']:5.1f}%  "
                  f"min={s['min']:6.1f}  avg={s['avg']:6.1f}  p50={s['p50']:6.1f}  "
                  f"p95={s['p95']:6.1f}  max={s['max']:6.1f}  ms")
        else:
            print(f"{label:4}    : no successful samples")
    j = jitter(rows)
    if j is not None:
        print(f"Jitter  : avg {j:.1f} ms")
    sigs = [r["signal"] for r in rows if r["signal"] is not None]
    if sigs:
        print(f"Signal  : avg {statistics.fmean(sigs):.0f}%  min {min(sigs)}%")


def plot(rows, out_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping plot (pip install matplotlib)")
        return
    t0 = rows[0]["t"]
    hrs = [(r["t"] - t0) / 3600 for r in rows]

    def series(key):
        return [r[key] if r[key] is not None else float("nan") for r in rows]

    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    fig.patch.set_facecolor("#0d1117")
    for ax, (key, label, color) in zip(axes, [
            ("wan", "WAN latency (ms)", "#4ade80"), ("dns", "DNS latency (ms)", "#c084fc"),
            ("signal", "WiFi signal (%)", "#60a5fa")]):
        ax.plot(hrs, series(key), color=color, linewidth=0.8)
        ax.set_ylabel(label)
        ax.set_facecolor("#161b22")
        ax.tick_params(colors="#8b949e")
        for sp in ax.spines.values():
            sp.set_color("#30363d")
        ax.grid(color="#21262d")
        if key != "signal":
            ax.set_yscale("log")
    axes[0].set_title("Network Monitor — logged session", color="#e6edf3")
    axes[-1].set_xlabel("hours elapsed", color="#8b949e")
    fig.tight_layout()
    fig.savefig(out_path, dpi=110, facecolor=fig.get_facecolor())
    print(f"Plot written to {out_path}")


def main():
    args = sys.argv[1:]
    out_png = None
    if "--plot" in args:
        i = args.index("--plot")
        out_png = args[i + 1] if i + 1 < len(args) else "network_report.png"
        del args[i:i + 2]
    paths = args or find_logs()
    if not paths:
        print("No logs found (looked next to the script and in ./logs). "
              "Enable logging in the app first.")
        return
    print(f"Analyzing {len(paths)} file(s): {', '.join(paths)}\n")
    rows = load(paths)
    report(rows)
    if out_png:
        plot(rows, out_png)


if __name__ == "__main__":
    main()
