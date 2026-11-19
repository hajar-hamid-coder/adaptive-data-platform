"""WCAG 2.x contrast check of the design tokens (frontend/mockups/tokens.css), both themes.

Usage: python3 scripts/check_contrast.py [--md]   (exit 1 if a pair fails)
"""

import re
import sys
from pathlib import Path

TOKENS = Path(__file__).resolve().parent.parent / "frontend" / "mockups" / "tokens.css"
BG_SURFACES = ("surface", "bg", "surface-2")
# (foreground, background, minimum ratio, usage)
PAIRS: list[tuple[str, str, float, str]] = [
    *((("text", s, 4.5, "texte courant") for s in BG_SURFACES)),
    *((("text-muted", s, 4.5, "texte secondaire") for s in BG_SURFACES)),
    ("on-accent", "accent", 4.5, "texte de bouton primaire"),
    ("accent", "surface", 4.5, "lien / texte accent"),
    ("accent", "bg", 4.5, "lien / texte accent"),
    ("focus", "surface", 3.0, "anneau de focus"),
    ("focus", "bg", 3.0, "anneau de focus"),
    ("border-strong", "surface", 3.0, "bordure de champ / contrôle"),
    ("border-strong", "bg", 3.0, "bordure de champ / contrôle"),
    *((f"{s}", f"{s}-bg", 4.5, f"badge {s}") for s in ("ok", "warn", "error", "info", "quarantine")),
    *((f"{s}", "surface", 4.5, f"texte {s}") for s in ("ok", "warn", "error", "info", "quarantine")),
    *((f"chart-{i}", "surface", 3.0, "série de graphique") for i in range(1, 6)),
]


def parse(css: str) -> dict[str, dict[str, str]]:
    """Light = first `:root` block, dark = `:root[data-theme="dark"]` block."""
    light = re.search(r':root, :root\[data-theme="light"\] \{(.*?)\n\}', css, re.S)
    dark = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S)
    if not light or not dark:
        raise SystemExit("tokens.css: bloc clair ou sombre introuvable")
    out = {}
    for name, m in (("clair", light), ("sombre", dark)):
        out[name] = dict(re.findall(r"--adp-([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))
    return out


def luminance(hex_: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check(css: str) -> list[tuple[str, str, str, float, float, str]]:
    """Rows (theme, fg, bg, ratio, minimum, usage); the dark theme must also mirror light keys."""
    themes = parse(css)
    if set(themes["clair"]) != set(themes["sombre"]):
        raise SystemExit("tokens.css: jeux de tokens clair/sombre différents")
    return [
        (t, fg, bg, ratio(tok[fg], tok[bg]), mn, use)
        for t, tok in themes.items()
        for fg, bg, mn, use in PAIRS
    ]


def main() -> int:
    rows = check(TOKENS.read_text(encoding="utf-8"))
    md = "--md" in sys.argv
    if md:
        print("| Thème | Premier plan | Fond | Ratio | Seuil | Usage | |\n|---|---|---|---|---|---|---|")
    bad = 0
    for t, fg, bg, r, mn, use in rows:
        ok = r >= mn
        bad += not ok
        line = (t, fg, bg, f"{r:.2f}", f"{mn}", use, "OK" if ok else "ÉCHEC")
        print(("| " + " | ".join(line) + " |") if md else "  ".join(line))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
