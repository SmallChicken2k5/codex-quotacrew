"""Set the Windows release version in a disposable fork build checkout."""

import re
import sys
from pathlib import Path

version = sys.argv[1]
if not re.fullmatch(r"1\.0\.[1-9][0-9]*", version) or int(version.split(".")[2]) > 65535:
    raise SystemExit("Invalid personal release version.")

replacements = {
    "pyproject.toml": (r'(?m)^version = "[0-9.]+"$', f'version = "{version}"'),
    "src/codex_account_manager/__init__.py": (
        r'(?m)^__version__ = "[0-9.]+"$',
        f'__version__ = "{version}"',
    ),
    "packaging/installer.iss": (
        r'(?m)^#define AppVersion "[0-9.]+"$',
        f'#define AppVersion "{version}"',
    ),
}
for name, (pattern, replacement) in replacements.items():
    file = Path(name)
    content, count = re.subn(pattern, replacement, file.read_text(encoding="utf-8"))
    if count != 1:
        raise SystemExit(f"Could not update the release version in {file}.")
    file.write_text(content, encoding="utf-8", newline="\n")
