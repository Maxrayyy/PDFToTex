import errno
from pathlib import Path

from texopt.pdf_batch import (
    build_lexoid_command,
    build_optimizer_command,
    discover_pdfs,
    optimized_work_path_for,
    output_path_for,
    publish_file,
    working_path_for,
)


def test_discover_pdfs_skips_hidden_files_and_directories(tmp_path):
    visible = tmp_path / "batch" / "record.PDF"
    hidden_file = tmp_path / "batch" / "._record.pdf"
    hidden_dir_file = tmp_path / ".audit" / "roundtrip.pdf"
    non_pdf = tmp_path / "batch" / "record.json"
    for path in (visible, hidden_file, hidden_dir_file, non_pdf):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")

    assert discover_pdfs(tmp_path) == [visible]


def test_output_path_preserves_structure_in_clean_optimized_directory(tmp_path):
    source_root = tmp_path / "U1"
    source = source_root / "batch" / "A31" / "record.pdf"
    output_root = tmp_path / "data" / "U1"

    assert output_path_for(source, source_root, output_root) == (
        output_root / "batch" / "A31" / "optimized" / "record.optimized.tex"
    )


def test_build_command_resumes_after_last_completed_page(tmp_path):
    source = tmp_path / "record.pdf"
    output = tmp_path / "record.tex"
    output.write_text(
        "page one\n% LEXOID_PAGE_COMPLETED: 1/3\n"
        "page two\n% LEXOID_PAGE_COMPLETED: 2/3\n",
        encoding="utf-8",
    )

    command = build_lexoid_command(source, output, "gpt-5.6-luna")

    assert command == [
        "lexoid",
        "latex",
        "--input",
        str(source),
        "--output",
        str(output),
        "--model",
        "gpt-5.6-luna",
        "--auto-orient",
        "--start-page",
        "3",
    ]


def test_working_path_keeps_raw_tex_out_of_data_directory(tmp_path):
    source_root = tmp_path / "Downloads" / "U1"
    source = source_root / "batch" / "record.pdf"
    work_root = tmp_path / "work" / "U1"

    assert working_path_for(source, source_root, work_root) == (
        work_root / "batch" / "record.tex"
    )


def test_optimizer_command_requires_compilation_and_repairs_only_failures(tmp_path):
    output_root = tmp_path / "data" / "U1"
    raw_output = tmp_path / "work" / "U1" / "batch" / "record.tex"
    final_output = output_root / "batch" / "optimized" / "record.optimized.tex"

    command = build_optimizer_command(
        raw_output,
        final_output,
        output_root,
        "gpt-5.6-luna",
    )

    assert command == [
        "texopt",
        "optimise",
        str(raw_output),
        "-o",
        str(optimized_work_path_for(raw_output)),
        "--registry",
        str(tmp_path / "work" / "U1" / "batch" / "record.fields.json"),
        "--report",
        str(tmp_path / "work" / "U1" / "batch" / "record.report.json"),
        "--log-file",
        str(output_root / "batch" / "_logs" / "record.texopt.log"),
        "--llm-model",
        "gpt-5.6-luna",
        "--name-cache",
        str(output_root / "_state" / "texopt-names.json"),
        "--syntax-repair-cache",
        str(output_root / "_state" / "texopt-syntax-repairs.json"),
        "--llm-repair-on-failure",
        "--compile-check",
    ]
    assert "--llm-syntax-repair" not in command


def test_publish_file_handles_cross_filesystem_sources(tmp_path, monkeypatch):
    source = tmp_path / "work-mount" / "record.optimized.tex"
    destination = tmp_path / "output-mount" / "record.tex"
    source.parent.mkdir()
    destination.parent.mkdir()
    source.write_text("optimized", encoding="utf-8")
    destination.write_text("raw", encoding="utf-8")
    original_replace = Path.replace

    def reject_cross_directory_replace(path, target):
        target = Path(target)
        if path.parent != target.parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", reject_cross_directory_replace)

    publish_file(source, destination)

    assert destination.read_text(encoding="utf-8") == "optimized"
    assert not source.exists()
    assert not destination.with_name(f".{destination.name}.tmp").exists()
