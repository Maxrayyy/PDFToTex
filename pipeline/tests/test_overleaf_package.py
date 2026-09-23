from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/package-overleaf-batch.py"


def run_package(publish, batch, outbox):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--publish-root",
            str(publish),
            "--batch-dir",
            str(batch),
            "--outbox",
            str(outbox),
        ],
        capture_output=True,
        text=True,
    )


def test_package_preserves_relative_paths_and_excludes_runtime_state(tmp_path):
    publish = tmp_path / "optimized"
    batch = publish / "A37Z201202605030"
    (batch / "assets").mkdir(parents=True)
    (batch / ".pipeline").mkdir()
    (batch / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
    (batch / "assets/stamp.png").write_bytes(b"png")
    (batch / ".pipeline/private.log").write_text("private", encoding="utf-8")
    (batch / "secret.env").write_text("TOKEN=secret", encoding="utf-8")
    outbox = tmp_path / "outbox"

    result = run_package(publish, batch, outbox)

    assert result.returncode == 0, result.stderr
    with ZipFile(outbox / "A37Z201202605030.zip") as archive:
        names = set(archive.namelist())
    assert names == {"main.tex", "assets/stamp.png"}
    digest = (outbox / "A37Z201202605030.zip.sha256").read_text(encoding="utf-8")
    assert digest.endswith("  A37Z201202605030.zip\n")


def test_package_rejects_empty_batch(tmp_path):
    publish = tmp_path / "optimized"
    batch = publish / "EMPTY"
    batch.mkdir(parents=True)

    result = run_package(publish, batch, tmp_path / "outbox")

    assert result.returncode != 0
    assert "does not contain a TEX file" in result.stderr


def test_package_rejects_batch_without_tex(tmp_path):
    publish = tmp_path / "optimized"
    batch = publish / "NO-TEX"
    batch.mkdir(parents=True)
    (batch / "preview.pdf").write_bytes(b"pdf")

    result = run_package(publish, batch, tmp_path / "outbox")

    assert result.returncode != 0
    assert "does not contain a TEX file" in result.stderr
