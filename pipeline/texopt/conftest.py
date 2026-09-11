import pytest


@pytest.fixture(autouse=True)
def pipeline_model_settings(monkeypatch):
    for variable, model in {
        "LEXOID_MODEL": "gpt-test-vision",
        "RECONCILE_MODEL": "gpt-test-review",
        "TEXOPT_MODEL": "gpt-test-naming",
        "TEXOPT_REPAIR_MODEL": "gpt-test-repair",
    }.items():
        monkeypatch.setenv(variable, model)
