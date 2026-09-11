import pytest

from texopt.recognition.lexoid_job import new_job
from texopt.llm import LLMBatchNamer
from texopt.reconcile import FieldReconcileAdapter
from texopt.stages import BatchConfig, build_stage_commands
from texopt.syntax_repair import LLMSyntaxRepairer


def test_batch_resolves_each_model_at_construction(tmp_path, monkeypatch):
    roles = {
        "LEXOID_MODEL": "gpt-test-vision",
        "RECONCILE_MODEL": "gpt-test-review",
        "TEXOPT_MODEL": "gpt-test-naming",
        "TEXOPT_REPAIR_MODEL": "gpt-test-repair",
    }
    for key, value in roles.items():
        monkeypatch.setenv(key, value)
    config = BatchConfig()
    assert config == BatchConfig.from_env()
    stages = build_stage_commands(tmp_path / "input.pdf", tmp_path, tmp_path / "out", config)
    for stage, flag, variable in (
        (0, "--model", "LEXOID_MODEL"), (1, "--model", "RECONCILE_MODEL"),
        (2, "--llm-model", "TEXOPT_MODEL"), (2, "--repair-model", "TEXOPT_REPAIR_MODEL"),
    ):
        argv = stages[stage].argv
        assert argv[argv.index(flag) + 1] == roles[variable]
    monkeypatch.setenv("LEXOID_MODEL", "gpt-test-updated")
    assert BatchConfig().vision_model == "gpt-test-updated"
    assert BatchConfig(vision_model="gpt-explicit").vision_model == "gpt-explicit"


def test_namer_and_jobs_read_current_environment(tmp_path, monkeypatch):
    for suffix in ("first", "second"):
        monkeypatch.setenv("TEXOPT_MODEL", f"gpt-namer-{suffix}")
        monkeypatch.setenv("LEXOID_MODEL", f"gpt-vision-{suffix}")
        monkeypatch.setenv("RECONCILE_MODEL", f"gpt-review-{suffix}")
        assert LLMBatchNamer(tmp_path / "cache.json").model == f"gpt-namer-{suffix}"
        job = new_job(str(tmp_path), "input.pdf")
        assert job.argv()[job.argv().index("--model") + 1] == f"gpt-vision-{suffix}"
        assert FieldReconcileAdapter().model == f"gpt-review-{suffix}"
    assert LLMBatchNamer(tmp_path / "cache.json", model="gpt-explicit").model == "gpt-explicit"


def test_syntax_repair_uses_its_own_role(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXOPT_MODEL", "gpt-naming")
    monkeypatch.setenv("TEXOPT_REPAIR_MODEL", "gpt-repair")
    assert LLMSyntaxRepairer(tmp_path / "cache.json").model == "gpt-repair"
    assert LLMSyntaxRepairer(tmp_path / "cache.json", model="gpt-explicit").model == "gpt-explicit"


def test_offline_cli_does_not_require_model_settings(tmp_path, monkeypatch):
    from texopt import cli

    for variable in ("TEXOPT_MODEL", "TEXOPT_REPAIR_MODEL", "RECONCILE_MODEL", "LEXOID_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    for args in (["--help"], ["optimise", "--help"], ["reconcile", "--help"]):
        with pytest.raises(SystemExit) as result:
            cli.main(args)
        assert result.value.code == 0
    source = tmp_path / "input.tex"
    source.write_text("\\documentclass{article}\n\\begin{document}\ntext\n\\end{document}\n")
    assert cli.main(["optimise", str(source), "--no-llm", "--no-probe"]) == 0


def test_cli_keeps_naming_and_repair_models_separate(tmp_path, monkeypatch):
    from texopt import cli

    monkeypatch.setenv("TEXOPT_MODEL", "gpt-naming")
    monkeypatch.setenv("TEXOPT_REPAIR_MODEL", "gpt-repair")
    seen = []
    monkeypatch.setattr(cli, "cmd_optimise", lambda args: seen.append((args.llm_model, args.repair_model)) or 0)
    for extra in ([], ["--llm-model", "gpt-explicit-naming", "--repair-model", "gpt-explicit-repair"]):
        assert cli.main(["optimise", str(tmp_path / "input.tex"), "--llm-syntax-repair",
                         "--semantic-naming", "inline", *extra]) == 0
    assert seen == [("gpt-naming", "gpt-repair"), ("gpt-explicit-naming", "gpt-explicit-repair")]


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_model_has_no_paid_fallback(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TEXOPT_MODEL", raising=False)
    else:
        monkeypatch.setenv("TEXOPT_MODEL", value)
    with pytest.raises(ValueError, match="TEXOPT_MODEL"):
        LLMBatchNamer(tmp_path / "cache.json")
    assert LLMBatchNamer(tmp_path / "cache.json", model="gpt-explicit").model == "gpt-explicit"
