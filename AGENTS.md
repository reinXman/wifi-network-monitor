# AGENTS.md — notes for AI coding agents

Context for picking up work on this repo. Read this before making changes.

## What this is

**WiFi / Network Monitor** — a single-file Python network dashboard
(`network_monitor.py`), pure standard library (tkinter, ctypes, raw sockets).
No third-party dependencies, and keep it that way.

Files:

| File | Purpose |
|---|---|
| `network_monitor.py` | Everything: probes, GUI, headless mode, load tester |
| `network_monitor.pyw` | **Entry point for double-click** — windowless launcher |
| `run_admin.bat` | Elevated launch (UAC) for WiFi details on Windows 11 |
| `analyze_log.py` | Parses CSV logs in `logs/`; also writes `Network Monitor Log.html` |
| `log.html` | Standalone log analyzer: open/drop `netlog_*.csv`, renders the dashboard client-side |
| `LOAD_TEST_GUIDE.md` | How to use the load-testing feature |

There is a sibling repo at `D:/Github/dns-benchmark` (DNS Benchmark, same
author, similar stdlib-only style) — it shares the dark theme and approach but
is a separate project.

## Critical conventions (do not regress these)

### 1. No console windows — ever

The app is launched windowless via `pythonw.exe` (through `network_monitor.pyw`
or `run_admin.bat`). Consequences:

- **Every** `subprocess.run`/`Popen` call that can run on Windows MUST pass
  `creationflags=NO_WINDOW` (module-level constant near the top, defined as
  `getattr(subprocess, "CREATE_NO_WINDOW", 0)` — no-op on POSIX).
  Without it, console subprocesses (`route`, `netsh`, `ping`, …) flash a cmd
  window on every call.
- The `.pyw` launcher must stay in sync with the real script — it just does
  `runpy.run_path(..., run_name="__main__")`, so there's exactly one copy of
  the code. Don't duplicate logic into it.
- `run_admin.bat` uses `pythonw`, not `python`.
- GUI code paths must not `print()` — under pythonw `sys.stdout` is `None`.
  Prints are only OK in the `--no-gui` headless path (see `headless()`).

### 2. Graceful degradation

All probe failures must degrade gracefully (return `None`, show `---`) rather
than crash — e.g. no WiFi adapter, no elevation, offline. Keep it that way.

## How to test

```bash
python -m py_compile network_monitor.py        # syntax check
python network_monitor.py --no-gui             # headless 5s diagnostic run (safe, no GUI)
pythonw network_monitor.pyw                    # real GUI — remember to kill pythonw afterwards
```

There are no unit tests; `--no-gui` + a short `pythonw` launch check is the
accepted verification. Always `Stop-Process -Name pythonw` when done testing.

## Recent history (for context)

- `1158c50` — windowless launch: added `.pyw` launcher, `run_admin.bat` → pythonw
  (user complained about a console window behind the GUI on double-click)
- `b479a61` — `CREATE_NO_WINDOW` on all subprocess calls (user saw console
  windows flash on every gateway/WiFi poll)
- `1856b69` / `0d007a3` — load testing: mini-iperf serve mode, rude UDP blast,
  bufferbloat analysis (pure stdlib, no new subprocess calls)
- Earlier: custom probe target, live GUI target editor, Linux support

## Commit style

Short imperative subject lines, no body, commit straight to `main` and push.
Recent examples: "Add load testing: mini-iperf serve mode, rude UDP blast,
bufferbloat analysis", "Suppress console flashes from subprocess calls
(CREATE_NO_WINDOW)".
