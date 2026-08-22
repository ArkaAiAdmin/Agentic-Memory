#!/usr/bin/env python3
"""Build wheel artifact for agentic-memory."""

import subprocess
import sys
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent.parent
    print(f"[Build] Building agentic-memory wheel from {root}...")
    
    dist_dir = root / "dist"
    dist_dir.mkdir(exist_ok=True)
    
    # Run python -m build or setup.py bdist_wheel
    cmd = [sys.executable, "-m", "pip", "install", "build", "--quiet"]
    subprocess.run(cmd, check=False)
    
    build_cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)]
    res = subprocess.run(build_cmd, cwd=str(root))
    if res.returncode != 0:
        print("[Build] python -m build failed, trying setup.py bdist_wheel fallback...")
        fallback_cmd = [sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(dist_dir)]
        subprocess.run(fallback_cmd, cwd=str(root), check=True)
        
    wheels = list(dist_dir.glob("*.whl"))
    print(f"[✓] Built {len(wheels)} wheel(s):")
    for w in wheels:
        print(f"  - {w.name} ({w.stat().st_size:,} bytes)")

if __name__ == "__main__":
    main()
