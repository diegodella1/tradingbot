from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.main import _market_open_price
from bot.polymarket.models import MarketType, UpDownMarket
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


def _market(market_id: str, start: datetime) -> UpDownMarket:
    return UpDownMarket(
        market_id=market_id,
        question="Bitcoin Up or Down - 5 minute",
        slug="btc-updown-5m-1",
        market_type=MarketType.FIVE_MINUTE,
        start_time=start,
        end_time=start + timedelta(minutes=5),
    )


def _tick(conn, price: float, ts: datetime) -> None:
    conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (price, ts.isoformat()))


def test_open_price_uses_first_tick_after_start(settings):
    init_db(settings.sqlite_path)
    start = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _tick(conn, 100.0, start + timedelta(seconds=1))
        _tick(conn, 101.0, start + timedelta(seconds=30))
        conn.commit()
        price = _market_open_price(Repository(conn), _market("m1", start))
    assert price == 100.0


def test_open_price_falls_back_to_nearest_tick_when_started_late(settings):
    init_db(settings.sqlite_path)
    start = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        # Only ticks BEFORE the market start exist (bot started late).
        _tick(conn, 95.0, start - timedelta(seconds=90))
        _tick(conn, 99.0, start - timedelta(seconds=5))
        conn.commit()
        price = _market_open_price(Repository(conn), _market("m2", start))
    assert price == 99.0  # nearest to start, not the live price


def test_open_price_prefers_chainlink_price_to_beat(settings):
    import json

    from bot.btc.chainlink_feed import ChainlinkBtcFeed

    init_db(settings.sqlite_path)
    start = datetime.now(UTC) - timedelta(seconds=30)
    chainlink = ChainlinkBtcFeed()
    for offset, value in ((-5, 99990.0), (1, 100005.0), (10, 100100.0), (29, 100200.0)):
        chainlink.handle_message(
            json.dumps(
                {
                    "topic": "crypto_prices_chainlink",
                    "payload": {"symbol": "btc/usd", "timestamp": (start + timedelta(seconds=offset)).timestamp() * 1000, "value": value},
                }
            )
        )
    with connect(settings.sqlite_path) as conn:
        _tick(conn, 88888.0, start + timedelta(seconds=1))  # coinbase proxy must lose
        conn.commit()
        repo = Repository(conn)
        price = _market_open_price(repo, _market("m-cl", start), chainlink=chainlink)
        source = conn.execute("SELECT source FROM market_open_prices WHERE market_id = 'm-cl'").fetchone()["source"]
    assert price == 100005.0
    assert source == "chainlink"


def test_open_price_waits_for_chainlink_on_fresh_window(settings):
    import json

    from bot.btc.chainlink_feed import ChainlinkBtcFeed

    init_db(settings.sqlite_path)
    start = datetime.now(UTC) - timedelta(seconds=10)
    chainlink = ChainlinkBtcFeed()
    # Feed is fresh (tick 12s old) but only has ticks BEFORE the boundary.
    chainlink.handle_message(
        json.dumps(
            {
                "topic": "crypto_prices_chainlink",
                "payload": {"symbol": "btc/usd", "timestamp": (start - timedelta(seconds=2)).timestamp() * 1000, "value": 99000.0},
            }
        )
    )
    with connect(settings.sqlite_path) as conn:
        _tick(conn, 88888.0, start + timedelta(seconds=1))
        conn.commit()
        repo = Repository(conn)
        price = _market_open_price(repo, _market("m-wait", start), chainlink=chainlink)
        persisted = conn.execute("SELECT COUNT(*) FROM market_open_prices").fetchone()[0]
    assert price is None  # holds off instead of persisting the proxy
    assert persisted == 0


def test_open_price_is_persisted_and_stable(settings):
    init_db(settings.sqlite_path)
    start = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _tick(conn, 100.0, start + timedelta(seconds=1))
        conn.commit()
        repo = Repository(conn)
        first = _market_open_price(repo, _market("m3", start))
        # Add a later, different tick; persisted open price must not drift.
        _tick(conn, 200.0, start + timedelta(seconds=2))
        conn.commit()
        second = _market_open_price(repo, _market("m3", start))
    assert first == 100.0
    assert second == 100.0
