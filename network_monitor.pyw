#!/usr/bin/env python3
"""
Network Monitor — console-free launcher.

Double-clicking network_monitor.py opens a cmd window behind the GUI because
.py files run under python.exe (a console app). Files ending in .pyw run under
pythonw.exe instead — no console. This launcher just executes the real script
in the same folder, so there is only one copy of the code.
"""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "network_monitor.py"), run_name="__main__")
