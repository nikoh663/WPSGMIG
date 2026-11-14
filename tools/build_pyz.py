"""Build wpmigrate.pyz, a single file that runs with `python3 wpmigrate.pyz`."""

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    output = ROOT / "dist" / "wpmigrate.pyz"
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        staging_path = Path(staging)
        shutil.copytree(ROOT / "wpmigrate", staging_path / "wpmigrate", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(ROOT / "wpmigrate" / "__main__.py", staging_path / "__main__.py")
        zipapp.create_archive(staging_path, output, interpreter="/usr/bin/env python3", compressed=True)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
