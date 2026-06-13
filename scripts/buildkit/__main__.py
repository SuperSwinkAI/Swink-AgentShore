"""`python -m scripts.buildkit <subcommand> ...` entry point for the build spine.

Subcommands are landed incrementally; today only `version` is wired. The
platform build phases (`macos`, `windows`) will dispatch here as they are
ported off the shell scripts (see docs/design/build-pipeline-unification.md).
"""

from __future__ import annotations

import sys

from . import macos as macos_cmd
from . import verify as verify_cmd
from . import version as version_cmd
from . import windows as windows_cmd

_SUBCOMMANDS = {
    "version": version_cmd.main,
    "verify": verify_cmd.main,
    "macos": macos_cmd.main,
    "windows": windows_cmd.main,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(f"usage: python -m scripts.buildkit <{'|'.join(_SUBCOMMANDS)}> [options]")
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    handler = _SUBCOMMANDS.get(name)
    if handler is None:
        print(f"unknown subcommand: {name!r}", file=sys.stderr)
        print(f"available: {', '.join(_SUBCOMMANDS)}", file=sys.stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
