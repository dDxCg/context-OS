import ast
import re
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_ac1_wheel_packages_include_src_utils():
    content = PYPROJECT.read_text()
    match = re.search(
        r"\[tool\.hatch\.build\.targets\.wheel\]\s*\npackages = (\[[^\]]*\])",
        content,
    )

    packages = ast.literal_eval(match.group(1))

    assert "src/utils" in packages
