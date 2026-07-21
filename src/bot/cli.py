from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import typer

from bot.backtest import (
    bucket_breakdown,
    build_training_rows,
    load_samples,
    policy_config,
    policy_evidence,
    recommend,
    recommend_policy,
    recommended_env_lines,
    run_backtest,
    run_walk_forward_sweep,
)
from bot.config import Settings, get_settings
from bot.knowledge.rag import index_markdown, search
from bot.strategy.calibration import accuracy, fit_logistic, log_loss, walk_forward
from bot.learning.policy import generate_learning_report, persist_learning_recommendations
from bot.learning.versions import (
    activate_paper_experiment,
    apply_active_policy,
    auto_promote_best_candidate,
    ensure_baseline_policy,
    evaluate_and_transition,
    list_policies,
    register_candidate,
    rollback_paper_experiment,
)
from bot.live_loop import run_live_loop
from bot.main import configure_logging, run_paper_loop
from bot.monitoring.health import local_health
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient
from bot.polymarket.geoblock import GeoblockClient
from bot.polymarket.models import OutcomeSide
from bot.storage.db import connect, init_db, prune_old_data
from bot.storage.repositories import Repository


app = typer.Typer(no_args_is_help=True)


def _settings() -> Settings:
    configure_logging()
    settings = get_settings()
    init_db(settings.sqlite_path)
    return settings


@app.command()
def discover() -> None:
    """Find active BTC Up/Down 5m and 15m markets."""
    settings = _settings()

    async def _run() -> None:
        gamma = GammaClient(settings)
        clob = ClobClient(settings)
        try:
            discovered = await gamma.discover_btc_updown()
            for market_type, markets in discovered.items():
                typer.echo(f"\n{market_type.value}: {len(markets)} verified market(s)")
                for market in markets[:2]:
                    up = market.tokens[OutcomeSide.UP]
                    down = market.tokens[OutcomeSide.DOWN]
                    up_book = await clob.get_order_book(up.token_id, market.market_id)
                    down_book = await clob.get_order_book(down.token_id, market.market_id)
                    typer.echo(f"- {market.question}")
                    typer.echo(f"  market_id: {market.market_id}")
                    typer.echo(f"  event_id: {market.event_id or ''}")
                    typer.echo(f"  slug: {market.slug}")
                    typer.echo(f"  start/end: {market.start_time} -> {market.end_time}")
                    typer.echo(f"  active={market.active} closed={market.closed} resolved={market.resolved} verified={market.mapping_verified}")
                    typer.echo(f"  liquidity={market.liquidity:.2f} volume={market.volume:.2f}")
                    typer.echo(f"  UP token={up.token_id} bid/ask={up_book.best_bid}/{up_book.best_ask} spread={up_book.spread}")
                    typer.echo(f"  DOWN token={down.token_id} bid/ask={down_book.best_bid}/{down_book.best_ask} spread={down_book.spread}")
        finally:
            await gamma.close()
            await clob.close()

    asyncio.run(_run())


@app.command()
def paper(max_cycles: int | None = typer.Option(None, help="Stop after N cycles; useful for tests/smoke checks.")) -> None:
    """Run real-market paper trading. No real orders."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        active = ensure_baseline_policy(conn, settings)
        if active:
            evaluate_and_transition(conn, active["version"], settings)
        auto_promote_best_candidate(conn, settings)
        settings = apply_active_policy(settings, conn)
        if active_policy := next((item for item in list_policies(conn) if item["is_active"]), None):
            settings = settings.model_copy(update={"policy_version": active_policy["version"]})
        else:
            settings = settings.model_copy(update={"enable_experimental_strategy": False})
    try:
        asyncio.run(run_paper_loop(settings, max_cycles=max_cycles))
    except RuntimeError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


@app.command()
def backtest(
    size: float = typer.Option(1.0, min=0.0, help="Per-signal stake in USDC used to scale PnL."),
    buckets: bool = typer.Option(False, "--buckets", help="Show WR/PnL breakdown by price, time, market type and probability."),
    sweep: bool = typer.Option(False, "--sweep", help="Grid-search entry gates using the calibrated model."),
    target_wr: float = typer.Option(0.70, min=0.0, max=1.0, help="Minimum win rate for the sweep recommendation."),
) -> None:
    """Backtest recorded strategy signals against verified market outcomes (hold-to-resolution)."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        report = run_backtest(conn, settings, size_usdc=size)
        samples = load_samples(conn) if buckets else []
        training_rows = build_training_rows(conn) if sweep else []

    typer.echo("backtest (recorded signals vs verified outcomes)")
    typer.echo(f"- signals={report['sample_count']} resolved={report['resolved_count']} stake_usdc={report['size_usdc']:.2f}")
    for scope in ("all", "5m", "15m"):
        stats = report["summary"].get(scope)
        if not stats:
            continue
        typer.echo(
            f"- {scope}: signals={stats['signals']} resolved={stats['resolved']} "
            f"win_rate={_fmt_pct(stats['win_rate'])} pnl_usdc={stats['pnl_usdc']:.2f} "
            f"roi={_fmt_pct(stats['roi'])} avg_edge_cents={_fmt_num(stats['avg_edge_cents'])} "
            f"avg_conf={_fmt_num(stats['avg_confidence'])}"
        )
    typer.echo("- calibration (predicted -> actual win rate):")
    for bucket in report["calibration"]:
        if bucket["count"] == 0:
            continue
        typer.echo(
            f"  [{bucket['bin_low']:.1f},{bucket['bin_high']:.1f}) n={bucket['count']} "
            f"predicted={_fmt_num(bucket['predicted'])} actual={_fmt_num(bucket['actual'])}"
        )
    if buckets:
        _print_buckets(samples, settings, size)
    if sweep:
        _print_sweep(training_rows, settings)


def _print_buckets(samples, settings: Settings, size: float) -> None:
    breakdown = bucket_breakdown(samples, settings, size_usdc=size)
    labels = {
        "entry_price": "precio de entrada",
        "seconds_to_close": "segundos al cierre",
        "market_type": "tipo de mercado",
        "estimated_probability": "prob estimada",
    }
    for key, label in labels.items():
        typer.echo(f"- WR por {label}:")
        for row in breakdown[key]:
            typer.echo(f"  {row['bucket']}: n={row['n']} WR={_fmt_pct(row['win_rate'])} pnl={row['pnl_usdc']:+.2f}")


def _print_sweep(training_rows, settings: Settings) -> None:
    if not training_rows:
        typer.echo("sweep: no hay decisiones con outcome verificado en la base")
        raise typer.Exit(1)
    try:
        report = run_walk_forward_sweep(
            training_rows,
            settings.paper_taker_fee_rate,
            min_probabilities=(0.65, 0.70, 0.75, 0.80, 0.85),
            min_seconds=(180, 300, 420, 600),
            price_bands=((0.55, 0.69), (0.60, 0.69), (0.65, 0.69), (0.55, 0.90)),
            min_net_edges=(5.0, 8.0, 10.0, 12.0),
            settings=settings,
        )
    except ValueError as exc:
        typer.echo(f"sweep: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(
        f"- sweep out-of-sample: modelo entrenado con {report['train_markets']} mercados "
        f"({report['train_rows']} filas); gates evaluados en {report['test_markets']} mercados de test"
    )
    train_by_key = {_cell_key(cell): cell for cell in report["train_cells"]}
    ranked = sorted((cell for cell in report["test_cells"] if cell.trades > 0), key=lambda c: c.pnl_usdc, reverse=True)
    typer.echo("- top 10 por PnL out-of-sample (IS = in-sample con el mismo modelo):")
    for cell in ranked[:10]:
        train_cell = train_by_key.get(_cell_key(cell))
        in_sample = (
            f"IS n={train_cell.trades} WR={_fmt_pct(train_cell.win_rate)} pnl={train_cell.pnl_usdc:+.2f}"
            if train_cell
            else "IS --"
        )
        typer.echo(
            f"  p>={cell.min_probability} sec>={cell.min_seconds_to_close} "
            f"banda={cell.price_band[0]}-{cell.price_band[1]} net>={cell.min_net_edge_cents}c "
            f"solo15m={cell.only_15m} | OOS n={cell.trades} WR={_fmt_pct(cell.win_rate)} "
            f"pnl={cell.pnl_usdc:+.2f} freq={cell.trades_per_day:.1f}/d | {in_sample}"
        )
    best = recommend_policy(report["test_cells"], min_trades=20)
    if best is None:
        typer.echo("- sin recomendacion: ninguna celda 15m cumple PnL/PF/drawdown/ventanas y frecuencia 2-6/d")
        return
    typer.echo(
        f"- recomendado por OOS (WR={_fmt_pct(best.win_rate)}, n={best.trades}, "
        f"pnl={best.pnl_usdc:+.2f}, PF={best.profit_factor:.2f}, DD={best.max_drawdown_pct:.1%}, "
        f"freq={best.trades_per_day:.1f}/d); lineas .env:"
    )
    for line in recommended_env_lines(best):
        typer.echo(f"  {line}")


def _cell_key(cell) -> tuple:
    return (cell.min_probability, cell.min_seconds_to_close, cell.price_band, cell.min_net_edge_cents, cell.only_15m)


@app.command()
def calibrate(
    min_samples: int = typer.Option(500, min=1, help="Minimum training rows required to train."),
    output: str | None = typer.Option(None, help="Where to write the model JSON (defaults to settings path)."),
) -> None:
    """Train the market-anchored probability model on ALL recorded decisions (both sides).

    Uses walk-forward (chronological 80/20) validation and reports the market
    baseline so overfit models are visible before deploying.
    """
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        training_rows = build_training_rows(conn)
    if len(training_rows) < min_samples:
        typer.echo(f"not enough training rows to calibrate: have {len(training_rows)}, need {min_samples}")
        raise typer.Exit(1)

    rows = [row.features for row in training_rows]
    labels = [row.label for row in training_rows]
    typer.echo(f"training rows={len(rows)} (markets={len({row.market_id for row in training_rows})}) base_rate={sum(labels) / len(labels):.3f}")

    report = walk_forward(rows, labels)
    typer.echo("walk-forward (80/20 cronologico):")
    typer.echo(
        f"- train n={report['train_samples']} log_loss={report['train_log_loss']:.4f} | "
        f"test n={report['test_samples']} log_loss={report['test_log_loss']:.4f} acc={report['test_accuracy']:.3f}"
    )
    typer.echo(f"- baseline mercado (ask como prob): log_loss={report['market_log_loss']:.4f}")
    if report["test_log_loss"] >= report["market_log_loss"]:
        typer.echo("- ADVERTENCIA: el modelo NO supera al mercado out-of-sample; no conviene usarlo para tradear")
    typer.echo("- WR out-of-sample por cutoff:")
    for item in report["wr_by_cutoff"]:
        typer.echo(f"  p>={item['cutoff']:.2f}: n={item['n']} WR={_fmt_pct(item['win_rate'])}")

    model = fit_logistic(rows, labels)
    destination = output or str(settings.probability_model_path)
    model.save(destination)
    typer.echo(f"- full-data log_loss={log_loss(model, rows, labels):.4f} accuracy={accuracy(model, rows, labels):.3f}")
    typer.echo(f"- weights={[round(w, 3) for w in model.weights]} bias={round(model.bias, 3)}")
    typer.echo(f"- saved model to {destination}")
    typer.echo("enable it by keeping ENABLE_EXPERIMENTAL_STRATEGY=true; the strategy loads it automatically")


@app.command("maker-sim")
def maker_sim(
    fill_window: float = typer.Option(60.0, min=1.0, help="Seconds a resting maker order stays before cancel."),
) -> None:
    """Compare maker (post at bid, zero fee) vs taker (actual fills) EV on settled paper trades."""
    from bot.backtest.maker import maker_vs_taker

    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        result = maker_vs_taker(conn, fill_window_seconds=fill_window)
    if result.trades == 0:
        typer.echo("maker-sim: no hay trades liquidados con snapshots para simular")
        raise typer.Exit(1)
    typer.echo(f"maker vs taker (ventana de fill={fill_window:.0f}s, {result.trades} trades liquidados)")
    typer.echo(f"- taker real:  pnl={result.taker_pnl_usdc:+.2f} fees={result.taker_fees_usdc:.2f}")
    typer.echo(
        f"- maker sim:   pnl={result.maker_pnl_usdc:+.2f} fees=0.00 "
        f"fills={result.maker_fills}/{result.trades} ({_fmt_pct(result.fill_rate)})"
    )
    typer.echo("- nota: el pnl maker es piso (sin rebate del 20% ni fills intra-snapshot)")
    delta = result.maker_pnl_usdc - result.taker_pnl_usdc
    typer.echo(f"- delta maker-taker: {delta:+.2f} USDC")


@app.command("backfill-outcomes")
def backfill_outcomes_cmd(limit: int = typer.Option(200, min=1, help="Max closed markets to refetch from Gamma.")) -> None:
    """Refetch closed markets without a verified winner and settle pending positions."""
    from bot.polymarket.backfill import backfill_outcomes
    from bot.storage.db import refresh_settlements

    settings = _settings()

    async def _run() -> dict:
        gamma = GammaClient(settings)
        try:
            with connect(settings.sqlite_path) as conn:
                result = await backfill_outcomes(gamma, conn, limit=limit)
                refresh_settlements(conn)
            return result
        finally:
            await gamma.close()

    result = asyncio.run(_run())
    typer.echo(f"backfill outcomes: pending={result['pending']} refreshed={result['refreshed']} verified={result['verified']}")
    for error in result["errors"]:
        typer.echo(f"- error: {error}")


@app.command()
def prune(
    retention_days: int | None = typer.Option(None, min=1, help="Retention window; defaults to DATA_RETENTION_DAYS."),
    vacuum: bool = typer.Option(False, "--vacuum", help="Run VACUUM afterwards to reclaim disk space (slow, needs free space)."),
) -> None:
    """Delete market snapshots and BTC ticks older than the retention window."""
    settings = _settings()
    days = retention_days if retention_days is not None else settings.data_retention_days
    with connect(settings.sqlite_path) as conn:
        result = prune_old_data(conn, days)
    typer.echo(f"prune (retention={days}d, cutoff={result['cutoff']})")
    typer.echo(f"- market_snapshots deleted={result['market_snapshots_deleted']}")
    typer.echo(f"- btc_ticks deleted={result['btc_ticks_deleted']}")
    if vacuum:
        with connect(settings.sqlite_path) as conn:
            conn.execute("VACUUM")
        typer.echo("- vacuum done")


@app.command("rag-index")
def rag_index() -> None:
    """Index local Markdown/Obsidian-style notes into SQLite FTS5."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        count = index_markdown(conn, settings)
    typer.echo(f"indexed_or_updated={count}")


@app.command("rag-search")
def rag_search(query: str, limit: int = typer.Option(8, min=1, max=20)) -> None:
    """Search indexed reference notes."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        for result in search(conn, query, limit=limit):
            typer.echo(f"- {result.title}\n  {result.source_path}\n  {result.snippet}")


@app.command()
def learn(note: str = typer.Option(..., help="Operational learning note."), tags: str = typer.Option("manual", help="Comma-separated tags.")) -> None:
    """Save a local learning note for later RAG/reference."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        Repository(conn).save_learning_note(note, tags)
    typer.echo("learning note saved")


@app.command("wallet-check")
def wallet_check() -> None:
    """Check Polymarket wallet/API readiness without placing orders."""
    settings = _settings()
    blockers: list[str] = []
    if not settings.live_auth_ready:
        blockers.append("missing CLOB/private key envs")
    if not settings.polymarket_funder_address:
        blockers.append("missing POLYMARKET_FUNDER_ADDRESS")
    if not settings.polymarket_deposit_wallet_address:
        blockers.append("missing POLYMARKET_DEPOSIT_WALLET_ADDRESS")
    try:
        import py_clob_client  # type: ignore  # noqa: F401
    except Exception as exc:
        blockers.append(f"py-clob-client-v2 unavailable: {exc}")

    geoblock = asyncio.run(GeoblockClient(settings).check())
    if geoblock.blocked:
        blockers.append(f"geoblock: {geoblock.reason}")

    if blockers:
        typer.echo("wallet readiness: blocked")
        for blocker in blockers:
            typer.echo(f"- {blocker}")
        raise typer.Exit(1)
    typer.echo("wallet readiness: ok")


@app.command("analyze-paper")
def analyze_paper() -> None:
    """Summarize paper strategy quality from SQLite."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        orders = conn.execute("SELECT COUNT(*), COALESCE(SUM(filled_size_usdc), 0) FROM orders").fetchone()
        fills = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_usdc), 0) FROM fills").fetchone()
        pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl_usdc), 0) FROM positions").fetchone()
        open_positions = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(p.size_usdc), 0)
            FROM positions p
            LEFT JOIN markets m ON m.market_id = p.market_id
            WHERE p.status = 'OPEN' AND (m.end_time IS NULL OR m.end_time > ?)
            """,
            (datetime.now(UTC).isoformat(),),
        ).fetchone()
        decisions = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   AVG(edge) AS avg_edge,
                   AVG(kelly_fraction) AS avg_kelly,
                   AVG(confidence) AS avg_confidence
            FROM strategy_decisions
            """
        ).fetchone()
        reasons = conn.execute(
            """
            SELECT reason, COUNT(*) AS count
            FROM strategy_decisions
            GROUP BY reason
            ORDER BY count DESC
            LIMIT 8
            """
        ).fetchall()

    typer.echo("paper analysis")
    typer.echo(f"- orders={int(orders[0])} filled_usdc={float(orders[1]):.2f}")
    typer.echo(f"- fills={int(fills[0])} volume_usdc={float(fills[1]):.2f}")
    typer.echo(f"- open_positions={int(open_positions[0])} open_cost_usdc={float(open_positions[1]):.2f}")
    typer.echo(f"- realized_pnl_usdc={float(pnl[0]):.2f}")
    typer.echo(
        f"- decisions={int(decisions['count'] or 0)} avg_edge_cents={float(decisions['avg_edge'] or 0) * 100:.2f} "
        f"avg_kelly={float(decisions['avg_kelly'] or 0):.4f} avg_confidence={float(decisions['avg_confidence'] or 0):.3f}"
    )
    typer.echo("- top reasons:")
    for row in reasons:
        typer.echo(f"  {row['count']}x {row['reason']}")


@app.command("learning-report")
def learning_report(persist: bool = typer.Option(True, help="Persist generated recommendations to SQLite.")) -> None:
    """Generate deterministic paper-learning recommendations. Does not change bot config."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        report = generate_learning_report(conn, settings)
        inserted = persist_learning_recommendations(conn, report["recommendations"]) if persist else 0

    summary = report["summary"]
    typer.echo("learning report")
    typer.echo(f"- mode={report['mode']} enabled={report['enabled']}")
    typer.echo(
        f"- settlements={summary['sample_size']} win_rate={_fmt_pct(summary['win_rate'])} "
        f"pnl_usdc={float(summary['pnl_usdc'] or 0):.2f} roi={_fmt_pct(summary['roi'])}"
    )
    typer.echo(f"- recommendations={len(report['recommendations'])} persisted={inserted}")
    for item in report["recommendations"]:
        typer.echo(f"  [{item['status']}] {item['scope']} / {item['metric']}: {item['recommendation']}")
        typer.echo(f"    sample={item['sample_size']} confidence={float(item['confidence']):.2f} reason={item['rationale']}")


@app.command("policy-optimize")
def policy_optimize(
    version: str = typer.Option("btc-updown-v4-oos-15m", help="Immutable candidate version."),
    register: bool = typer.Option(False, help="Register and auto-activate the eligible candidate in paper."),
    evidence_output: Path | None = typer.Option(None, help="Evidence JSON path."),
) -> None:
    """Select a full-gate 15m policy targeting 2-6 paper entries/day."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        training_rows = build_training_rows(conn)
    if not training_rows:
        typer.echo("no verified training rows")
        raise typer.Exit(1)
    report = run_walk_forward_sweep(
        training_rows,
        settings.paper_taker_fee_rate,
        min_probabilities=(0.65, 0.70, 0.75, 0.80, 0.85),
        min_seconds=(180, 300, 420, 600),
        price_bands=((0.55, 0.69), (0.60, 0.69), (0.65, 0.69), (0.55, 0.90)),
        min_net_edges=(5.0, 8.0, 10.0, 12.0),
        settings=settings,
    )
    best = recommend_policy(report["test_cells"], min_trades=20)
    if best is None:
        typer.echo("no eligible policy: OOS safety/frequency gates failed")
        raise typer.Exit(1)
    config = policy_config(best)
    evidence = policy_evidence(best, report)
    destination = evidence_output or Path("artifacts/policy-evidence") / f"{version}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence_bytes = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    destination.write_bytes(evidence_bytes)
    typer.echo(
        f"eligible {version}: n={best.trades} pnl={best.pnl_usdc:+.2f} "
        f"PF={best.profit_factor:.2f} DD={best.max_drawdown_pct:.1%} freq={best.trades_per_day:.1f}/d"
    )
    typer.echo(f"config={json.dumps(config, sort_keys=True)}")
    typer.echo(f"evidence={destination}")
    if not register:
        return
    model_path = settings.probability_model_path
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest() if model_path.exists() else None
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            version,
            config,
            evidence,
            evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            model_sha256=model_sha256,
        )
        decision = auto_promote_best_candidate(conn, settings)
    if decision is None or decision.status != "paper_active":
        typer.echo(f"candidate registered but not activated: {decision.reason if decision else 'not eligible'}")
        raise typer.Exit(1)
    typer.echo(f"paper activation: {decision.reason}")


@app.command("policy-register")
def policy_register(
    version: str = typer.Option(..., help="Immutable policy version identifier."),
    config_json: str = typer.Option(..., help="JSON object containing paper-only config overrides."),
    evidence_file: Path = typer.Option(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    model_file: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False, readable=True),
) -> None:
    """Register an OOS-tested candidate. It can only become active in paper."""
    settings = _settings()
    try:
        config = json.loads(config_json)
        if not isinstance(config, dict):
            raise ValueError("config-json must be an object")
        evidence_bytes = evidence_file.read_bytes()
        oos_metrics = json.loads(evidence_bytes)
        if not isinstance(oos_metrics, dict):
            raise ValueError("evidence-file must contain a JSON object")
        model_sha256 = hashlib.sha256(model_file.read_bytes()).hexdigest() if model_file else None
        with connect(settings.sqlite_path) as conn:
            register_candidate(
                conn,
                version,
                config,
                oos_metrics,
                evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
                model_sha256=model_sha256,
            )
            decision = auto_promote_best_candidate(conn, settings)
    except (json.JSONDecodeError, ValueError) as exc:
        typer.echo(f"candidate rejected: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"registered {version}")
    typer.echo(f"auto-promotion: {decision.reason if decision else 'not eligible or active policy exists'}")


@app.command("policy-status")
def policy_status(evaluate: bool = typer.Option(False, help="Evaluate and transition the active paper policy.")) -> None:
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        policies = list_policies(conn)
        if evaluate:
            for item in policies:
                if item["status"] == "paper_active":
                    evaluate_and_transition(conn, item["version"], settings)
            auto_promote_best_candidate(conn, settings)
            policies = list_policies(conn)
        report = generate_learning_report(conn, settings)
    metrics_by_version = {item["version"]: item for item in report["policy_versions"]}
    for item in policies:
        metrics = metrics_by_version.get(item["version"], {}).get("metrics", {})
        typer.echo(
            f"{item['version']} status={item['status']} trades={metrics.get('trades', 0)} "
            f"pnl={float(metrics.get('pnl_usdc') or 0):+.2f} drawdown={_fmt_pct(metrics.get('max_drawdown_pct'))}"
        )


@app.command("policy-experiment-activate")
def policy_experiment_activate(version: str = typer.Option(..., help="Registered maker experiment version.")) -> None:
    """Explicitly activate a guarded maker policy in paper; live must remain off."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        decision = activate_paper_experiment(conn, version, settings)
    typer.echo(f"{version}: {decision.status} - {decision.reason}")
    if decision.status != "paper_active":
        raise typer.Exit(1)


@app.command("policy-experiment-rollback")
def policy_experiment_rollback(version: str = typer.Option(..., help="Maker experiment version to stop.")) -> None:
    """Stop the experiment, cancel its pending maker orders and restore the prior policy."""
    settings = _settings()
    with connect(settings.sqlite_path) as conn:
        restored = rollback_paper_experiment(conn, version)
    typer.echo(f"stopped {version}; restored={restored or 'none'}")


def _fmt_pct(value) -> str:
    return "--" if value is None else f"{float(value) * 100:.1f}%"


def _fmt_num(value) -> str:
    return "--" if value is None else f"{float(value):.3f}"


@app.command()
def live(max_cycles: int | None = typer.Option(None, help="Stop after N cycles; useful for tests/smoke checks.")) -> None:
    """Run the live trading loop, only if explicitly enabled and safety checks pass."""
    settings = _settings()
    if not settings.enable_live_trading:
        typer.echo("ENABLE_LIVE_TRADING=false; live mode blocked")
        raise typer.Exit(1)
    if not settings.live_auth_ready:
        typer.echo("live credentials incomplete; live mode blocked")
        raise typer.Exit(1)
    if settings.require_live_confirmation:
        confirmed = typer.confirm("Live trading can place real Polymarket orders. Type confirmation required. Continue?")
        if not confirmed:
            typer.echo("live mode canceled")
            raise typer.Exit(1)
    geoblock = asyncio.run(GeoblockClient(settings).check())
    if geoblock.blocked:
        typer.echo(f"geoblock blocked live mode: {geoblock.reason}")
        raise typer.Exit(1)
    typer.echo("live startup checks passed; starting live loop (Ctrl+C to stop)")
    configure_logging(settings)
    asyncio.run(run_live_loop(settings, max_cycles=max_cycles))


@app.command()
def health() -> None:
    """Print local and API readiness checks."""
    settings = _settings()

    async def _run() -> None:
        for item in local_health(settings):
            typer.echo(f"{item.name}: {'ok' if item.ok else 'blocked'} - {item.detail}")
        geoblock = await GeoblockClient(settings).check()
        typer.echo(f"geoblock: {'blocked' if geoblock.blocked else 'ok'} - {geoblock.reason}")
        clob = ClobClient(settings)
        try:
            server_time = await clob.get_server_time()
            skew = abs(datetime.now(UTC).timestamp() - server_time)
            typer.echo(f"clob_time: ok - skew={skew:.3f}s")
        except Exception as exc:
            typer.echo(f"clob_time: blocked - {exc}")
        finally:
            await clob.close()
        typer.echo("market_feed: unknown until paper/live run starts")
        typer.echo("btc_feed: unknown until paper/live run starts")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
