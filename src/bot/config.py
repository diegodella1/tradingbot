from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    polymarket_private_key: str = ""
    polymarket_clob_api_key: str = ""
    polymarket_clob_secret: str = ""
    polymarket_clob_passphrase: str = ""
    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_chain_id: int = 137
    gamma_host: str = "https://gamma-api.polymarket.com"
    geoblock_url: str = "https://polymarket.com/api/geoblock"

    enable_live_trading: bool = False
    enable_experimental_strategy: bool = False
    require_live_confirmation: bool = True

    max_position_usdc: float = 5.0
    max_market_position_usdc: float = 5.0
    max_daily_loss_usdc: float = 4.0
    max_open_markets: int = 1
    max_spread_cents: float = 4.0
    min_orderbook_liquidity_usdc: float = 50.0
    max_consecutive_losses: int = 3
    loss_streak_window_minutes: int = 120
    cooldown_after_loss_seconds: int = 300
    max_trades_per_market: int = 1
    min_seconds_to_close: int = 45
    min_seconds_to_close_5m: int | None = None
    min_seconds_to_close_15m: int | None = None
    clock_skew_max_seconds: float = 2.0

    price_feed: Literal["coinbase"] = "coinbase"
    enable_websocket_feeds: bool = False
    websocket_book_max_age_seconds: float = 5.0
    btc_symbol: str = "BTC-USD"
    market_types: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["5m", "15m"])
    kill_switch_file: Path = Path("./KILL_SWITCH")
    sqlite_path: Path = Path("./bot.sqlite3")
    paper_slippage_cents: float = 1.0
    paper_fill_ratio: float = 0.75
    paper_enable_fees: bool = True
    paper_taker_fee_rate: float = 0.07
    paper_order_style: Literal["taker", "maker"] = "taker"
    paper_maker_fill_window_seconds: int = 60
    paper_maker_bid_offset_cents: float = Field(default=0.0, ge=0.0, le=99.0)
    paper_max_trade_size_usdc: float = 5.0
    paper_loop_interval_seconds: float = 10.0
    paper_bankroll_usdc: float = 10.0
    paper_trade_size_usdc: float = 1.0
    live_bankroll_usdc: float = 10.0
    live_trade_size_usdc: float = 1.0
    live_loop_interval_seconds: float = 3.0
    alert_webhook_url: str = ""
    dashboard_admin_token: str = ""
    deploy_commit: str = "unknown"
    data_retention_days: int = 7
    outcome_backfill_cycles: int = 60
    regime_window_trades: int = 50
    regime_min_trades: int = 30
    enable_regime_stop: bool = False
    paper_experiment_enabled: bool = False
    paper_experiment_stop_loss_usdc: float = 1.0
    paper_experiment_max_drawdown_pct: float = 0.01
    paper_experiment_min_fills: int = 20
    paper_experiment_min_profit_factor: float = 1.10
    paper_experiment_min_fill_rate: float = 0.50
    live_order_style: Literal["taker", "maker"] = "taker"
    min_edge_cents: float = 3.0
    min_confidence: float = 0.70
    min_estimated_probability: float = 0.60
    max_realized_volatility: float = 0.001
    min_abs_change_since_open: float = 0.0
    min_entry_price: float = 0.10
    min_entry_price_15m: float = 0.65
    max_entry_price: float = 0.90
    min_profit_if_win_usdc: float = 0.65
    min_net_edge_cents: float = 5.0
    min_probability_15m: float = 0.70
    min_probability_5m: float = 0.75
    min_net_edge_15m_cents: float = 8.0
    min_net_edge_5m_cents: float = 10.0
    enable_5m_scout: bool = False
    min_confidence_5m: float = 0.80
    min_book_imbalance_5m: float = 0.10
    max_trade_pct_15m: float = 0.02
    max_trade_pct_5m: float = 0.0075
    max_trades_per_hour: int = 2
    max_trades_per_day: int = 6
    disable_5m_after_recent_loss_usdc: float = 2.0
    recent_5m_loss_lookback: int = 10
    danger_zone_min_price: float = 0.70
    danger_zone_max_price: float = 0.75
    danger_zone_min_probability: float = 0.78
    danger_zone_min_net_edge_cents: float = 12.0
    high_price_min_probability: float = 0.82
    high_price_min_net_edge_cents: float = 10.0
    size_tier_base_usdc: float = 0.75
    size_tier_good_usdc: float = 1.0
    size_tier_strong_usdc: float = 1.5
    size_tier_max_usdc: float = 2.0
    size_tier_good_probability: float = 0.74
    size_tier_good_net_edge_cents: float = 10.0
    size_tier_strong_probability: float = 0.80
    size_tier_strong_net_edge_cents: float = 12.0
    size_tier_max_probability: float = 0.84
    size_tier_max_net_edge_cents: float = 15.0
    drawdown_lookback_trades: int = 10
    drawdown_pause_loss_usdc: float = 3.0
    drawdown_pause_seconds: int = 14400
    drawdown_size_multiplier: float = 0.5
    min_book_imbalance: float = 0.05
    kelly_fraction_multiplier: float = 0.25
    min_kelly_size_usdc: float = 1.0
    max_token_position_usdc: float = 5.0
    hold_to_resolution: bool = True
    enable_exit_signals: bool = False
    exit_min_probability: float = 0.40
    probability_model_path: Path = Path("./probability_model.json")
    cancel_unfilled_after_seconds: int = 20
    enable_learning_recommendations: bool = True
    policy_version: str = "btc-updown-v3-break-even"
    min_break_even_margin_cents: float = 0.0
    paper_auto_promote: bool = True
    policy_min_forward_trades: int = 200
    policy_min_profit_factor: float = 1.10
    policy_max_drawdown_pct: float = 0.15
    policy_require_evidence_hash: bool = True
    rag_paths: Annotated[list[Path], NoDecode] = Field(default_factory=lambda: [Path("./README.md"), Path("./docs")])
    rag_obsidian_vault_path: Path | None = None
    polymarket_signature_type: int = 3
    polymarket_funder_address: str = ""
    polymarket_deposit_wallet_address: str = ""

    @field_validator("market_types", mode="before")
    @classmethod
    def parse_market_types(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return list(value or [])

    @field_validator("rag_paths", mode="before")
    @classmethod
    def parse_rag_paths(cls, value: object) -> list[Path]:
        if isinstance(value, str):
            return [Path(item.strip()) for item in value.split(",") if item.strip()]
        return [Path(item) for item in (value or [])]

    @field_validator("rag_obsidian_vault_path", mode="before")
    @classmethod
    def parse_optional_path(cls, value: object) -> Path | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return Path(str(value))

    @property
    def live_auth_ready(self) -> bool:
        return all(
            [
                self.polymarket_private_key,
                self.polymarket_clob_api_key,
                self.polymarket_clob_secret,
                self.polymarket_clob_passphrase,
            ]
        )

    def minimum_seconds_to_close_for(self, market_type: str) -> int:
        if market_type == "5m" and self.min_seconds_to_close_5m is not None:
            return self.min_seconds_to_close_5m
        if market_type == "15m" and self.min_seconds_to_close_15m is not None:
            return self.min_seconds_to_close_15m
        return self.min_seconds_to_close

    def minimum_probability_for(self, market_type: str) -> float:
        if market_type == "5m":
            return max(self.min_estimated_probability, self.min_probability_5m)
        if market_type == "15m":
            return max(self.min_estimated_probability, self.min_probability_15m)
        return self.min_estimated_probability

    def minimum_net_edge_cents_for(self, market_type: str) -> float:
        if market_type == "5m":
            return max(self.min_net_edge_cents, self.min_net_edge_5m_cents)
        if market_type == "15m":
            return max(self.min_net_edge_cents, self.min_net_edge_15m_cents)
        return self.min_net_edge_cents

    def minimum_confidence_for(self, market_type: str) -> float:
        if market_type == "5m":
            return max(self.min_confidence, self.min_confidence_5m)
        return self.min_confidence

    def minimum_book_imbalance_for(self, market_type: str) -> float:
        if market_type == "5m":
            return max(self.min_book_imbalance, self.min_book_imbalance_5m)
        return self.min_book_imbalance

    def max_trade_pct_for(self, market_type: str) -> float:
        if market_type == "5m":
            return self.max_trade_pct_5m
        if market_type == "15m":
            return self.max_trade_pct_15m
        return min(self.max_trade_pct_15m, self.max_trade_pct_5m)

    def minimum_entry_price_for(self, market_type: str) -> float:
        if market_type == "15m":
            return max(self.min_entry_price, self.min_entry_price_15m)
        return self.min_entry_price

    def price_bucket_requirements(self, market_type: str, price: float) -> tuple[float, float]:
        min_probability = self.minimum_probability_for(market_type)
        min_net_edge_cents = self.minimum_net_edge_cents_for(market_type)
        if market_type != "15m":
            return min_probability, min_net_edge_cents
        if self.danger_zone_min_price <= price < self.danger_zone_max_price:
            min_probability = max(min_probability, self.danger_zone_min_probability)
            min_net_edge_cents = max(min_net_edge_cents, self.danger_zone_min_net_edge_cents)
        elif price >= self.danger_zone_max_price:
            min_probability = max(min_probability, self.high_price_min_probability)
            min_net_edge_cents = max(min_net_edge_cents, self.high_price_min_net_edge_cents)
        return min_probability, min_net_edge_cents

    def size_tier_usdc(self, probability: float, net_edge_cents: float) -> float:
        if probability >= self.size_tier_max_probability and net_edge_cents >= self.size_tier_max_net_edge_cents:
            return self.size_tier_max_usdc
        if probability >= self.size_tier_strong_probability and net_edge_cents >= self.size_tier_strong_net_edge_cents:
            return self.size_tier_strong_usdc
        if probability >= self.size_tier_good_probability and net_edge_cents >= self.size_tier_good_net_edge_cents:
            return self.size_tier_good_usdc
        return self.size_tier_base_usdc

    def strategy_config_snapshot(self) -> dict:
        """Safe, non-secret config snapshot stored with new paper decisions."""
        keys = (
            "policy_version",
            "enable_experimental_strategy",
            "enable_5m_scout",
            "min_confidence",
            "min_estimated_probability",
            "min_probability_15m",
            "min_probability_5m",
            "min_entry_price",
            "min_entry_price_15m",
            "max_entry_price",
            "min_edge_cents",
            "min_net_edge_cents",
            "min_net_edge_15m_cents",
            "min_net_edge_5m_cents",
            "min_break_even_margin_cents",
            "market_types",
            "max_spread_cents",
            "min_orderbook_liquidity_usdc",
            "min_seconds_to_close",
            "min_seconds_to_close_5m",
            "min_seconds_to_close_15m",
            "max_trades_per_hour",
            "max_trades_per_day",
            "paper_bankroll_usdc",
            "paper_trade_size_usdc",
            "paper_enable_fees",
            "paper_taker_fee_rate",
            "paper_order_style",
            "paper_maker_fill_window_seconds",
            "paper_maker_bid_offset_cents",
            "paper_max_trade_size_usdc",
            "paper_experiment_enabled",
            "paper_experiment_stop_loss_usdc",
            "paper_experiment_max_drawdown_pct",
            "paper_experiment_min_fills",
            "paper_experiment_min_profit_factor",
            "paper_experiment_min_fill_rate",
            "kelly_fraction_multiplier",
            "hold_to_resolution",
        )
        return {key: getattr(self, key) for key in keys}


def get_settings() -> Settings:
    return Settings()
