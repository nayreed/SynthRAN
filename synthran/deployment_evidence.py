"""Command-line live deployment evidence validator."""

from __future__ import annotations

import argparse

from .acceptance import validate_live_evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m synthran.deployment_evidence")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        validate_live_evidence(
            args.candidate,
            args.evidence,
            max_age_seconds=args.max_age_seconds,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
