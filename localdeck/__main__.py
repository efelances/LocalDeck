"""CLI and desktop entrypoint for LocalDeck."""

from __future__ import annotations

import argparse

from localdeck import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localdeck", description="LocalDeck")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
