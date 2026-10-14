"""Run with host admission; publication never starts research or broker work."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from tree_options.research.experiments.build import build
from tree_options.research.experiments.io import Unavailable, load, publish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-release", required=True)
    args = parser.parse_args()
    config, _ = load(args.config)
    # The command owns only its output, never any source tree or configuration.
    output = args.out.resolve()
    sources = config.get("sources", {})
    if not isinstance(sources, dict):
        raise Unavailable("source_configuration_invalid")
    protected = [
        args.config.resolve(),
        *[Path(v).resolve() for v in sources.values() if isinstance(v, str)],
    ]
    if any(output == p or p in output.parents for p in protected):
        raise Unavailable("publication_overlaps_source")
    index = build(config, datetime.now(UTC), args.source_release)
    publish(index, args.out)
    missing = sum(s.status == "unavailable" for s in index.sources)
    print(
        f"Published {len(index.experiments)} records; {missing} unavailable sources; no execution authority."
    )
    # A successful publication may still report failed prerequisites to timers.
    return 3 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
