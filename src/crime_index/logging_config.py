from __future__ import annotations

import logging
import os


def configure_logging(level: str | None = None) -> None:
    resolved_level = (level or os.getenv("CRIME_INDEX_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, resolved_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
