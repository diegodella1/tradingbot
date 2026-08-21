from __future__ import annotations

import hashlib

from typer.testing import CliRunner

import bot.cli as cli_module
from bot.storage.db import connect, init_db
from bot.strategy.calibration import FEATURE_NAMES, ProbabilityModel


def _approved_model() -> ProbabilityModel:
    return ProbabilityModel(
        weights=[0.0] * (len(FEATURE_NAMES) - 1),
        bias=0.0,
        means=[0.0] * (len(FEATURE_NAMES) - 1),
        stds=[1.0] * (len(FEATURE_NAMES) - 1),
        schema_version=2,
        training_metadata={
            "data_start": "2026-01-01T00:00:00+00:00",
            "data_end": "2026-02-01T00:00:00+00:00",
            "distinct_markets": 120,
            "dataset_sha256": "a" * 64,
            "oos": {
                "test_markets": 30,
                "test_log_loss": 0.50,
                "market_log_loss": 0.60,
                "test_brier_score": 0.18,
                "market_brier_score": 0.22,
                "beats_market": True,
            },
        },
    )


def test_model_promote_is_atomic_and_audited(monkeypatch, settings, tmp_path):
    init_db(settings.sqlite_path)
    settings.probability_model_path.write_text("old-model", encoding="utf-8")
    candidate_path = tmp_path / "candidate.json"
    _approved_model().save(candidate_path)
    candidate_bytes = candidate_path.read_bytes()
    expected_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    monkeypatch.setattr(cli_module, "_settings", lambda: settings)

    result = CliRunner().invoke(
        cli_module.app,
        ["model-promote", "--candidate-model", str(candidate_path)],
    )

    assert result.exit_code == 0, result.output
    assert settings.probability_model_path.read_bytes() == candidate_bytes
    assert settings.probability_model_path.with_name("probability_model.previous.json").read_text(
        encoding="utf-8"
    ) == "old-model"
    with connect(settings.sqlite_path) as conn:
        event = conn.execute(
            "SELECT event_type, evidence_sha256 FROM policy_evolution_events WHERE event_key = ?",
            (f"model:{expected_sha256}:promoted",),
        ).fetchone()
    assert event["event_type"] == "model_promoted"
    assert event["evidence_sha256"] == expected_sha256
