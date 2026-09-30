import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runner():
    script = Path(__file__).resolve().parents[2] / "scripts/run-sequential-queue.py"
    spec = importlib.util.spec_from_file_location("sequential_queue", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_queue_uses_environment_configuration_without_model_pin(tmp_path, monkeypatch):
    runner = load_runner()
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"container": "example", "sources": []}))
    config = SimpleNamespace(vision_model="gpt-custom", fallback_model="gpt-other",
                             publish_root="/custom/published", render_dpi=300,
                             vision_concurrency=3, reconcile_concurrency=1)
    monkeypatch.setattr(runner.BatchConfig, "from_env", lambda: config)
    monkeypatch.setattr(runner.sys, "argv", ["queue", str(queue), "--prepare-only"])
    assert runner.main() == 0
    assert not queue.with_suffix(".status.json").exists()

    config.vision_concurrency = 0
    with pytest.raises(ValueError, match="must be positive"):
        runner.main()
