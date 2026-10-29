#!/usr/bin/env python3
"""PreToolUse(Write|Edit|MultiEdit) : protège le backlog, les secrets et les garde-fous. Exit 2 = refus."""
import json
import sys
from pathlib import Path

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path", "")
claude = Path(__file__).resolve().parent.parent
autopilot = (claude / "state" / "autopilot").exists()
p = Path(path)

if p.name == "backlog.xlsx":
    print("BLOQUÉ : éditer le backlog via `python3 scripts/backlog.py` (set / validate / hypo-add).", file=sys.stderr)
    sys.exit(2)
if p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example":
    print("BLOQUÉ : ne jamais écrire de secrets ; modifier .env.example avec des valeurs factices.", file=sys.stderr)
    sys.exit(2)
if autopilot and (claude / "hooks") in p.parents or autopilot and p == claude / "settings.json":
    print("BLOQUÉ : les garde-fous (.claude/hooks, settings.json) ne se modifient pas en mode autopilot.", file=sys.stderr)
    sys.exit(2)
