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
    max_daily_loss_usdc: float = 10.0
    max_open_markets: int = 1
    max_spread_cents: float = 4.0
    min_orderbook_liquidity_usdc: float = 50.0
    max_consecutive_losses: int = 3
    cooldown_after_loss_seconds: int = 300
    max_trades_per_market: int = 1
    min_seconds_to_close: int = 45
    clock_skew_max_seconds: float = 2.0

    price_feed: Literal["coinbase"] = "coinbase"
    btc_symbol: str = "BTC-USD"
    market_types: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["5m", "15m"])
    kill_switch_file: Path = Path("./KILL_SWITCH")
    sqlite_path: Path = Path("./bot.sqlite3")
    paper_slippage_cents: float = 1.0
    paper_fill_ratio: float = 0.75
    paper_enable_fees: bool = True
    paper_taker_fee_rate: float = 0.07
    paper_loop_interval_seconds: float = 10.0
    paper_bankroll_usdc: float = 10.0
    paper_trade_size_usdc: float = 1.0
    live_bankroll_usdc: float = 10.0
    live_trade_size_usdc: float = 1.0
    min_edge_cents: float = 3.0
    min_confidence: float = 0.65
    min_entry_price: float = 0.10
    max_entry_price: float = 0.90
    min_profit_if_win_usdc: float = 0.70
    min_net_edge_cents: float = 5.0
    min_book_imbalance: float = 0.05
    kelly_fraction_multiplier: float = 0.25
    min_kelly_size_usdc: float = 1.0
    max_token_position_usdc: float = 1.0
    hold_to_resolution: bool = True
    cancel_unfilled_after_seconds: int = 20
    enable_learning_recommendations: bool = True
    rag_paths: Annotated[list[Path], NoDecode] = Field(default_factory=lambda: [Path("./README.md"), Path("./docs"), Path("/home/diego/Documents/diegodella")])
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


def get_settings() -> Settings:
    return Settings()
