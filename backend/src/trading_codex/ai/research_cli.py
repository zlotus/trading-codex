import argparse
import json
from collections.abc import Sequence

from trading_codex.ai.research import load_research_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading-codex-ai-research",
        description="Validate physically isolated AI research data splits.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate a research manifest")
    validate.add_argument("manifest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        dataset = load_research_manifest(args.manifest)
        dataset.verify()
        print(
            json.dumps(
                {
                    "status": "passed",
                    "version": dataset.version,
                    "fingerprint": dataset.fingerprint,
                    "splits": {
                        partition.split.value: {
                            "root": str(partition.root),
                            "start_date": partition.start_date.isoformat(),
                            "end_date": partition.end_date.isoformat(),
                            "content_sha256": partition.content_sha256,
                        }
                        for partition in (
                            dataset.train,
                            dataset.validation,
                            dataset.test,
                        )
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
