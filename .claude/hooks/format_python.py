#!/usr/bin/env python3
"""PostToolUse(Write|Edit) : formate les fichiers Python avec ruff si disponible. Jamais bloquant."""
import json
import shutil
import subprocess
import sys

path = (json.load(sys.stdin).get("tool_input") or {}).get("file_path", "")
ruff = shutil.which("ruff")
if path.endswith(".py") and ruff:
    subprocess.run([ruff, "format", "--quiet", path], check=False)
    subprocess.run([ruff, "check", "--fix", "--quiet", path], check=False)
sys.exit(0)
