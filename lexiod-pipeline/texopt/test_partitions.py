"""Static PDF partitions retain checkpoints and publish into one clean tree."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading

import pytest

from .stages import BatchConfig, artifact_paths, run_batch


def sources(tmp_path):
    root = tmp_path / "input"
    sizes = {"OOX/started.pdf": 2, "A31/record.pdf": 8, "A32/record.pdf": 7,
             "A33/one.pdf": 6, "A34/two.pdf": 5, "A35/broken.pdf": None}
    for name in sizes:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pdf")

    def page_counter(path):
        pages = sizes[path.relative_to(root).as_posix()]
        if pages is None:
            raise ValueError("Invalid PDF")
        return pages

    return root, sizes, page_counter


def completed_stage(stage, log):
    for path in stage.outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".tex":
            path.write_text("page\n% LEXOID_PAGE_COMPLETED: 1/1\n")
        elif path.name.endswith(".recognition.json"):
            path.write_text(json.dumps({"schema": "recognition/v1", "pages": [{"page": 1}]}))
        elif path.name.endswith(".report.json"):
            path.write_text(json.dumps({"compile_check": {"ok": True, "passes": 2},
                "layout_check": {"ok": True, "actual_pages": 1, "expected_pages": 1,
                    "page_map": [{"source_page": 1, "start": 1, "end": 1}]}}))
        else:
            path.write_text("{}")
    return 0


def test_plan_balances_pages_pins_existing_work_and_accounts_for_bad_pdfs(tmp_path):
    from .partitions import create_plan, validate_plan

    root, sizes, counter = sources(tmp_path)
    work = tmp_path / "data/U1"
    paths = artifact_paths(root / "OOX/started.pdf", root, work)
    paths["log_dir"].mkdir(parents=True)
    plan = create_plan(root, work, tmp_path / "data/optimized/U1", 2, page_counter=counter)
    validate_plan(plan)
    workers = plan["workers"]
    assigned = [name for worker in workers for name in worker["files"]]
    assert len(assigned) == len(set(assigned)) == len(sizes)
    assert set(assigned) == set(sizes)
    assert "OOX/started.pdf" in workers[0]["files"]
    assert workers[0]["output_root"] == str(work)
    assert workers[1]["output_root"] == str(work / ".workers/02")
    assert sum(worker["pages"] for worker in workers) == 28
    assert abs(workers[0]["pages"] - workers[1]["pages"]) <= 8
    assert plan["unreadable"] == [{"source": "A35/broken.pdf", "error": "Invalid PDF"}]


@pytest.mark.parametrize("damage", ["duplicate", "missing", "escape", "shared_work", "shared_publish"])
def test_partition_validation_rejects_incomplete_or_overlapping_queues(tmp_path, damage):
    from .partitions import create_plan, validate_plan

    root, _, counter = sources(tmp_path)
    plan = create_plan(root, tmp_path / "work", tmp_path / "final", 2, page_counter=counter)
    bad = deepcopy(plan)
    if damage == "duplicate":
        bad["workers"][1]["files"].append(bad["workers"][0]["files"][0])
    elif damage == "missing":
        bad["workers"][0]["files"].pop()
    elif damage == "escape":
        bad["workers"][0]["files"][0] = "../outside.pdf"
    elif damage == "shared_work":
        bad["workers"][1]["output_root"] = bad["workers"][0]["output_root"]
    else:
        bad["publish_root"] = bad["workers"][1]["output_root"]
    with pytest.raises(ValueError):
        validate_plan(bad)


def test_two_partitions_run_concurrently_and_keep_completed_stages(tmp_path):
    from .partitions import create_plan, run_partition

    root = tmp_path / "input"
    for name in ("A31/same.pdf", "A32/same.pdf", "OOX/done.pdf"):
        path = root / name
        path.parent.mkdir(parents=True)
        path.write_bytes(b"pdf")
    work, final = tmp_path / "work", tmp_path / "final"
    config = BatchConfig(publish_root=str(final))
    assert run_batch(root, work, config, completed_stage, lambda p: 1,
                     include_paths=["OOX/done.pdf"]) == 0
    plan = create_plan(root, work, final, 2, page_counter=lambda p: 1)
    rendezvous = threading.Barrier(2)
    visited = []

    def runner(stage, log):
        assert "done" not in str(stage.outputs[0])
        if stage.stage == "recognize":
            visited.append(str(stage.inputs[0].relative_to(root)))
            rendezvous.wait(timeout=5)
        return completed_stage(stage, log)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_partition, plan, worker["id"], config=config,
                               runner=runner, page_counter=lambda p: 1)
                   for worker in plan["workers"]]
        assert [f.result() for f in futures] == [0, 0]
    assert sorted(visited) == ["A31/same.pdf", "A32/same.pdf"]
    assert sorted(p.relative_to(final).as_posix() for p in final.rglob("*") if p.is_file()) == [
        "A31/same.tex", "A32/same.tex", "OOX/done.tex"]
    for worker in plan["workers"]:
        assert (Path(worker["output_root"]) / ".state/pipeline.sqlite3").is_file()
        assert (Path(worker["output_root"]) / "manifest.json").is_file()
