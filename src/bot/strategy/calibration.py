from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Direction-adjusted feature order shared by training and inference.
#
# The model is ANCHORED to the market: z = logit(implied) + w . extras + b,
# where implied is the ask of the chosen side (its market-implied probability).
# Walk-forward validation on production data showed the free-form model
# overfits (OOS log-loss worse than the market price alone), while the
# anchored form beats the market baseline.
#
# Time matters a lot in up/down markets: the same $20 move is far more
# decisive with 40s left than with 4 minutes left (historical WR ~39% under
# 180s to close vs 85-90% above 300s), so the move is also expressed per unit
# of remaining time (move_per_sqrt_sec) and normalized by volatility
# (vol_norm_move).
#
# `implied` must stay LAST: predict/fit treat the leading entries as weighted
# extras and the final entry as the anchor.
FEATURE_NAMES = [
    "m15",
    "m60",
    "open_move",
    "volatility",
    "book",
    "move_per_sqrt_sec",
    "vol_norm_move",
    "implied",
]

# Centers tanh() for typical values of change/(vol*sqrt(sec)) observed in
# production data (median ~1.2e4). Standardization absorbs the rest.
_VOL_NORM_SCALE = 25000.0


def build_features(
    momentum_15s: float,
    momentum_60s: float,
    change_since_open: float,
    realized_volatility: float,
    book_imbalance: float,
    implied: float,
    sign: int,
    seconds_to_close: float = 0.0,
) -> list[float]:
    """Build the direction-adjusted feature vector for one side (sign +1 UP / -1 DOWN)."""
    seconds = max(seconds_to_close, 1.0)
    sqrt_seconds = math.sqrt(seconds)
    signed_move = sign * change_since_open
    vol_norm_move = math.tanh(signed_move / (max(realized_volatility, 1e-5) * sqrt_seconds * _VOL_NORM_SCALE))
    return [
        sign * momentum_15s,
        sign * momentum_60s,
        signed_move,
        realized_volatility,
        book_imbalance,
        signed_move / sqrt_seconds,
        vol_norm_move,
        implied,
    ]


def _sigmoid(z: float) -> float:
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _logit(probability: float) -> float:
    clamped = min(max(probability, 1e-6), 1 - 1e-6)
    return math.log(clamped / (1 - clamped))


@dataclass
class ProbabilityModel:
    """Market-anchored logistic model: z = logit(implied) + w . standardized(extras) + b."""

    weights: list[float]
    bias: float
    means: list[float]
    stds: list[float]
    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    trained_samples: int = 0
    anchored: bool = True
    schema_version: int = 1
    training_metadata: dict = field(default_factory=dict)

    def predict_proba(self, features: list[float]) -> float:
        # weights/means/stds cover the extras (all but the trailing anchor), so
        # zip() intentionally stops before the implied entry.
        z = (_logit(features[-1]) if self.anchored else 0.0) + self.bias
        for weight, value, mean, std in zip(self.weights, features, self.means, self.stds, strict=False):
            z += weight * ((value - mean) / (std or 1.0))
        return _sigmoid(z)

    def is_compatible(self) -> bool:
        return list(self.feature_names) == FEATURE_NAMES and len(self.weights) == len(FEATURE_NAMES) - 1

    def is_trade_approved(self) -> bool:
        oos = self.training_metadata.get("oos") or {}
        dataset_sha256 = str(self.training_metadata.get("dataset_sha256") or "")
        try:
            distinct_markets = int(self.training_metadata.get("distinct_markets") or 0)
            test_markets = int(oos.get("test_markets") or 0)
            model_log_loss = float(oos["test_log_loss"])
            market_log_loss = float(oos["market_log_loss"])
            model_brier = float(oos["test_brier_score"])
            market_brier = float(oos["market_brier_score"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            self.schema_version >= 2
            and self.is_compatible()
            and distinct_markets >= 30
            and bool(self.training_metadata.get("data_start"))
            and bool(self.training_metadata.get("data_end"))
            and len(dataset_sha256) == 64
            and all(character in "0123456789abcdef" for character in dataset_sha256.lower())
            and bool(oos.get("beats_market"))
            and test_markets >= 30
            and model_log_loss < market_log_loss
            and model_brier < market_brier
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "ProbabilityModel":
        return cls(**json.loads(text))

    def save(self, path: Path | str) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "ProbabilityModel | None":
        file_path = Path(path)
        if not file_path.exists():
            return None
        try:
            return cls.from_json(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


def _standardize(rows: list[list[float]]) -> tuple[list[float], list[float]]:
    count = len(rows)
    features = len(rows[0])
    means = [sum(row[j] for row in rows) / count for j in range(features)]
    stds: list[float] = []
    for j in range(features):
        variance = sum((row[j] - means[j]) ** 2 for row in rows) / count
        stds.append(math.sqrt(variance) or 1.0)
    return means, stds


def fit_logistic(
    rows: list[list[float]],
    labels: list[int],
    epochs: int = 400,
    lr: float = 0.5,
    l2: float = 0.1,
) -> ProbabilityModel:
    """Fit the anchored logistic model via batch gradient descent (pure Python).

    `rows` are full feature vectors (see FEATURE_NAMES); the trailing implied
    price acts as a fixed logit offset, so the extras only learn the residual
    edge over the market. The relatively strong default L2 comes from a
    walk-forward sweep on production data.
    """
    if not rows:
        raise ValueError("no training rows")
    count = len(rows)
    extras = [row[:-1] for row in rows]
    anchors = [_logit(row[-1]) for row in rows]
    features = len(extras[0])
    means, stds = _standardize(extras)
    scaled = [[(row[j] - means[j]) / stds[j] for j in range(features)] for row in extras]
    weights = [0.0] * features
    bias = 0.0
    for _ in range(epochs):
        grad_w = [0.0] * features
        grad_b = 0.0
        for values, anchor, label in zip(scaled, anchors, labels, strict=False):
            z = anchor + bias
            for j in range(features):
                z += weights[j] * values[j]
            error = _sigmoid(z) - label
            for j in range(features):
                grad_w[j] += error * values[j]
            grad_b += error
        for j in range(features):
            weights[j] -= lr * (grad_w[j] / count + l2 * weights[j])
        bias -= lr * (grad_b / count)
    return ProbabilityModel(
        weights=weights,
        bias=bias,
        means=means,
        stds=stds,
        feature_names=list(FEATURE_NAMES),
        trained_samples=count,
    )


def _log_loss_probs(probabilities: list[float], labels: list[int]) -> float:
    if not probabilities:
        return 0.0
    total = 0.0
    for probability, label in zip(probabilities, labels, strict=False):
        clamped = min(max(probability, 1e-9), 1 - 1e-9)
        total += -(label * math.log(clamped) + (1 - label) * math.log(1 - clamped))
    return total / len(probabilities)


def probability_log_loss(probabilities: list[float], labels: list[int]) -> float:
    return _log_loss_probs(probabilities, labels)


def log_loss(model: ProbabilityModel, rows: list[list[float]], labels: list[int]) -> float:
    return _log_loss_probs([model.predict_proba(row) for row in rows], labels)


def brier_score(probabilities: list[float], labels: list[int]) -> float:
    if not probabilities:
        return 0.0
    return sum((probability - label) ** 2 for probability, label in zip(probabilities, labels, strict=False)) / len(probabilities)


def accuracy(model: ProbabilityModel, rows: list[list[float]], labels: list[int]) -> float:
    if not rows:
        return 0.0
    correct = sum(1 for values, label in zip(rows, labels, strict=False) if (model.predict_proba(values) >= 0.5) == bool(label))
    return correct / len(rows)


def walk_forward(
    rows: list[list[float]],
    labels: list[int],
    train_fraction: float = 0.8,
    cutoffs: tuple[float, ...] = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80),
) -> dict:
    """Chronological train/test validation. `rows` must already be time-sorted.

    Reports out-of-sample log-loss versus the market baseline (using the
    implied price alone) and the realized win rate at each model-probability
    cutoff, so thresholds are chosen on unseen data.
    """
    cut = max(1, min(len(rows) - 1, int(len(rows) * train_fraction)))
    model = fit_logistic(rows[:cut], labels[:cut])
    test_rows, test_labels = rows[cut:], labels[cut:]
    probabilities = [model.predict_proba(row) for row in test_rows]
    market_probabilities = [row[-1] for row in test_rows]

    wr_by_cutoff = []
    for cutoff in cutoffs:
        picked = [label for probability, label in zip(probabilities, test_labels, strict=False) if probability >= cutoff]
        wr_by_cutoff.append(
            {
                "cutoff": cutoff,
                "n": len(picked),
                "win_rate": (sum(picked) / len(picked)) if picked else None,
            }
        )
    return {
        "train_samples": cut,
        "test_samples": len(test_rows),
        "train_log_loss": log_loss(model, rows[:cut], labels[:cut]),
        "test_log_loss": _log_loss_probs(probabilities, test_labels),
        "market_log_loss": _log_loss_probs(market_probabilities, test_labels),
        "test_brier_score": brier_score(probabilities, test_labels),
        "market_brier_score": brier_score(market_probabilities, test_labels),
        "test_accuracy": sum(1 for p, label in zip(probabilities, test_labels, strict=False) if (p >= 0.5) == bool(label)) / len(test_rows) if test_rows else 0.0,
        "wr_by_cutoff": wr_by_cutoff,
    }
