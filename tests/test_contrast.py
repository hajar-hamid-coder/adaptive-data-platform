"""Design tokens meet WCAG AA in both themes (P5-05); the dark media block mirrors data-theme."""

import importlib.util
import re
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_contrast.py"
spec = importlib.util.spec_from_file_location("check_contrast", SCRIPT)
assert spec and spec.loader
cc = importlib.util.module_from_spec(spec)
sys.modules["check_contrast"] = cc
spec.loader.exec_module(cc)


def test_all_pairs_pass_aa() -> None:
    rows = cc.check(cc.TOKENS.read_text(encoding="utf-8"))
    assert len(rows) >= 56
    assert [r for r in rows if r[3] < r[4]] == []


def test_ratio_known_values() -> None:
    assert round(cc.ratio("#000000", "#ffffff"), 1) == 21.0
    assert cc.ratio("#777777", "#ffffff") < 4.5


def test_prefers_dark_block_mirrors_explicit_dark() -> None:
    css = cc.TOKENS.read_text(encoding="utf-8")
    dark = dict(
        re.findall(
            r"--adp-([\w-]+):\s*(#[0-9a-f]{6})",
            re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1),
        )
    )  # type: ignore[union-attr]
    media = re.search(r"@media \(prefers-color-scheme: dark\) \{(.*?)\n\}\n", css, re.S)
    assert media
    assert dict(re.findall(r"--adp-([\w-]+):\s*(#[0-9a-f]{6})", media.group(1))) == dark
