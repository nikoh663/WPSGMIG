"""Allow ``python3 -m wpmigrate`` and ``python3 wpmigrate.pyz``."""

import sys

if sys.version_info < (3, 10):
    sys.stderr.write(
        "This tool needs Python 3.10 or newer, but this server runs Python %d.%d.\n"
        "Ask your host how to run a newer python3 (it is often called python3.10, python3.11 or python3.12).\n"
        % sys.version_info[:2]
    )
    sys.exit(15)

from wpmigrate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
