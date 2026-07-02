from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import typer

from bot.config import Settings, get_settings
from bot.knowledge.rag import index_markdown, search
from bot.learning.policy import generate_learning_report, persist_learning_recommendations
from bot.main import configure_logging, run_paper_loop
from bot.monitoring.health import local_health
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient
from bot.polymarket.geoblock import GeoblockClient
from bot.polymarket.models import OutcomeSide
from bot.storage.db import init_db
from bot.storage.db import connect
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
    try:
        asyncio.run(run_paper_loop(settings, max_cycles=max_cycles))
    except RuntimeError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


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


def _fmt_pct(value) -> str:
    return "--" if value is None else f"{float(value) * 100:.1f}%"


@app.command()
def live() -> None:
    """Run live mode only if explicitly enabled and safety checks pass."""
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
    typer.echo("live startup checks passed; long-running live loop is not started by this scaffold command")


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
