"""Export a compact spectator game from validated historical model receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

from tree_options.desk.trade_floor import build_replay, project_replay


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    game = project_replay(build_replay(args.run))
    raw = (json.dumps(game, indent=2, sort_keys=True) + "\n").encode()
    out = args.out.expanduser().resolve(strict=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise FileExistsError(out)
    fd, temporary = tempfile.mkstemp(prefix=".trade-floor-", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, out)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({"out": str(out), "rounds": len(game["rounds"]),
                      "windows": len(game["windows"]),
                      "sha256": hashlib.sha256(raw).hexdigest(),
                      "execution_enabled": game["execution_enabled"]}, sort_keys=True))


if __name__ == "__main__":
    main()
