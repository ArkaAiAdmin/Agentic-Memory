#!/usr/bin/env python3
"""Backward-compat shim: delegates to cron/cron_cleanup_auto_logs.py.

Z-7 fix (rename cleanup_auto_logs.py → cron_cleanup_auto_logs.py).
This stub exists so any direct invocation of the old path still works.
Remove after one full release cycle.
"""
import os
import sys
import subprocess

_stub_dir = os.path.dirname(os.path.abspath(__file__))
_new_script = os.path.join(_stub_dir, "cron_cleanup_auto_logs.py")
sys.exit(subprocess.call([sys.executable, _new_script] + sys.argv[1:]))
