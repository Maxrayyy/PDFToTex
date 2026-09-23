#!/usr/bin/env python3
"""Create a reviewable Overleaf upload archive from one published batch."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from overleaf_publish import included_files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_batch(publish_root: Path, batch_dir: Path, outbox: Path) -> Path:
    publish_root = publish_root.resolve(strict=True)
    batch_dir = batch_dir.resolve(strict=True)
    if not batch_dir.is_dir() or not batch_dir.is_relative_to(publish_root):
        raise ValueError("batch directory must be inside the publish root")

    files = included_files(batch_dir)
    if not any(path.suffix.lower() == ".tex" for path in files):
        raise ValueError("batch does not contain a TEX file")

    outbox.mkdir(parents=True, exist_ok=True)
    destination = outbox / f"{batch_dir.name}.zip"
    digest_path = outbox / f"{batch_dir.name}.zip.sha256"

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{batch_dir.name}.", suffix=".zip.tmp", dir=outbox
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(batch_dir).as_posix())

        digest = sha256(temporary)
        if destination.exists() and sha256(destination) == digest:
            temporary.unlink()
        else:
            os.replace(temporary, destination)

        descriptor, digest_temporary_name = tempfile.mkstemp(
            prefix=f".{batch_dir.name}.", suffix=".sha256.tmp", dir=outbox
        )
        digest_temporary = Path(digest_temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"{digest}  {destination.name}\n")
            os.replace(digest_temporary, digest_path)
        finally:
            digest_temporary.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)

    return destination


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--publish-root", required=True, type=Path)
    result.add_argument("--batch-dir", required=True, type=Path)
    result.add_argument("--outbox", required=True, type=Path)
    return result


def main() -> int:
    argument_parser = parser()
    args = argument_parser.parse_args()
    try:
        archive = package_batch(args.publish_root, args.batch_dir, args.outbox)
    except (OSError, ValueError) as error:
        argument_parser.error(str(error))
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
