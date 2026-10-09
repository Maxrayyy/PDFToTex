#!/usr/bin/env python3
"""Start the optional Web API or independent worker; legacy entrypoints are untouched."""

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "pipeline")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=["serve", "worker"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.config:
        os.environ["PDFTOTEX_WEB_CONFIG"] = str(args.config.resolve())
    if args.service == "serve":
        import uvicorn

        uvicorn.run("web.backend.api:app", host=args.host, port=args.port)
    else:
        from web.backend.worker import main as worker

        sys.argv = [sys.argv[0]]
        worker()


if __name__ == "__main__":
    main()
