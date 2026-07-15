from bot.execution.risk_manager import RiskDecision
from bot.polymarket.models import Signal, SignalAction
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


def test_repeated_non_actionable_telemetry_is_coalesced(settings, market):
    init_db(settings.sqlite_path)
    signal = Signal(action=SignalAction.HOLD, reason="same reason")
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        for _ in range(3):
            repo.save_signal(market.market_id, signal)
            repo.save_strategy_decision(market, signal)
            repo.save_risk_event(market.market_id, RiskDecision(False, "same reason"))
            repo.save_health_event("paper_loop", "ok", "same detail")
            repo.save_learning_note("same note", "paper")
            repo.save_discovery_rejection("5m", "question", "slug", "same rejection")

        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "signals",
                "strategy_decisions",
                "risk_events",
                "health_events",
                "learning_notes",
                "discovery_rejection_rollups",
            )
        }
        occurrences = conn.execute(
            "SELECT occurrences FROM discovery_rejection_rollups"
        ).fetchone()[0]

    assert counts == {table: 1 for table in counts}
    assert occurrences == 3


def test_actionable_signals_are_never_coalesced(settings, market):
    init_db(settings.sqlite_path)
    signal = Signal(action=SignalAction.BUY_UP, confidence=0.9, size_usdc=1, reason="trade")
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        repo.save_signal(market.market_id, signal)
        repo.save_signal(market.market_id, signal)

        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
