"""Pre-commit hook: refuse files containing obvious secrets. Stdlib only."""

import re
import sys
from pathlib import Path

PATTERNS = {
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "PEM private key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
}
IGNORED = {".env.example"}


def scan(path: Path) -> list[str]:
    """Return the names of the secret patterns found in `path`."""
    if path.name in IGNORED:
        return []
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return []
    return [name for name, rx in PATTERNS.items() if rx.search(text)]


def main(argv: list[str]) -> int:
    bad = False
    for arg in argv:
        for name in scan(Path(arg)):
            print(f"{arg}: possible {name}", file=sys.stderr)
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
