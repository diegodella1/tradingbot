#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot.storage.maintenance import compact_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Create verified compact SQLite copy and backup.")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--backup-directory", type=Path, required=True)
    args = parser.parse_args()
    result = compact_database(args.source, args.output, args.backup_directory)
    print(json.dumps({key: str(value) if isinstance(value, Path) else value for key, value in result.items()}, sort_keys=True))


if __name__ == "__main__":
    main()
