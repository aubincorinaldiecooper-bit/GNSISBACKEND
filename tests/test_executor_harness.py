import pytest

from gnsis.service.executor.models import Budgets, RunSpec
from gnsis.service.executor.gateway import _estimate_cost
from gnsis.service.settings import DEFAULT_ALLOWED_MODELS, Settings, _executor_harness


def test_harness_defaults_to_openhands_and_accepts_omp():
    assert _executor_harness(None) == "openhands"
    assert _executor_harness(" OMP ") == "omp"
    with pytest.raises(ValueError):
        _executor_harness("other")


def test_harness_env_reaches_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("GNSIS_EXECUTOR_HARNESS", "omp")
    assert Settings.from_env().executor_harness == "omp"


def test_public_spec_carries_harness():
    spec = RunSpec(job_id="j", instruction="i", repository_full_name="o/r", repository_id=None, base_sha="s",
                   base_branch="main", model="moonshotai/kimi-k3", advisor_model=None,
                   allowed_models=["moonshotai/kimi-k3"], budgets=Budgets(1, 1, 1, 1.0),
                   model_gateway_url="https://api.example/v1/chat/completions", network_policy="p",
                   deadline_seconds=60, harness="omp")
    assert spec.to_public_dict()["harness"] == "omp"


def test_kimi_is_default_and_priced():
    assert DEFAULT_ALLOWED_MODELS[0] == "moonshotai/kimi-k3"
    assert _estimate_cost("moonshotai/kimi-k3", 1_000_000, 0) == pytest.approx(0.73)
