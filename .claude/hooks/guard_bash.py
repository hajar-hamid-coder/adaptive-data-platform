#!/usr/bin/env python3
"""PreToolUse(Bash) : second filet de sécurité en plus de permissions.deny. Exit 2 = refus."""
import json
import re
import sys
from pathlib import Path

cmd = (json.load(sys.stdin).get("tool_input") or {}).get("command", "")
state = Path(__file__).resolve().parent.parent / "state"
autopilot = (state / "autopilot").exists()

RULES = [
    (r"git\s+commit\b.*--no-verify|git\s+commit\b.*\s-n\b", "Contourner les hooks git (--no-verify) est interdit."),
    (r"git\s+push\b.*(--force|-f\b|--force-with-lease)", "Force-push interdit."),
    (r"git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f", "Commande destructive interdite : demander à l'utilisateur."),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\.\.?(/|\s|$))", "rm -rf sur un chemin dangereux."),
    (r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh", "Pipe curl | sh interdit : télécharger, lire, puis exécuter."),
    (r"(cat|echo|printf|tee|sed|cp|mv)\b[^|;&]*\.env(\s|$|\")", "Ne pas lire/écrire .env via le shell (utiliser .env.example)."),
]
for pattern, msg in RULES:
    if re.search(pattern, cmd):
        print(f"BLOQUÉ : {msg}", file=sys.stderr)
        sys.exit(2)

if "backlog.xlsx" in cmd and "scripts/backlog.py" not in cmd:
    if re.search(r"openpyxl|to_excel|sed\s+-i|\brm\b|\bmv\b|>\s*\S*backlog\.xlsx", cmd):
        print("BLOQUÉ : modifier backlog.xlsx uniquement via `python3 scripts/backlog.py`.", file=sys.stderr)
        sys.exit(2)

if autopilot and re.search(r"git\s+push\b[^|;&]*\b(main|master)\b", cmd):
    print("BLOQUÉ : pas de push sur main pendant une phase ; la fusion se fait après validation (/validate-phase).", file=sys.stderr)
    sys.exit(2)
