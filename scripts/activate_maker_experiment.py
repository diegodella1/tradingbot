from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bot.config import get_settings
from bot.learning.versions import activate_paper_experiment, register_candidate
from bot.storage.db import connect, init_db


VERSION = "btc-updown-v4-maker-experiment"
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "artifacts" / "policy-evidence" / f"{VERSION}.json"
CONFIG = ROOT / "artifacts" / "policy-evidence" / f"{VERSION}.config.json"


def main() -> None:
    settings = get_settings()
    if settings.enable_live_trading:
        raise SystemExit("refusing activation: ENABLE_LIVE_TRADING must be false")
    evidence_bytes = EVIDENCE.read_bytes()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_bytes)
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            VERSION,
            config,
            evidence,
            evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
        )
        decision = activate_paper_experiment(conn, VERSION, settings)
    print(f"{VERSION}: {decision.status} - {decision.reason}")
    if decision.status != "paper_active":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
