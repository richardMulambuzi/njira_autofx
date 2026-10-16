import logging
from pathlib import Path

from dotenv import load_dotenv

# Load .env at the EARLIEST import point. The Django settings module is
# imported as `forex.settings`, so this package's __init__ runs BEFORE the
# settings body — any module-level boot hook here needs the env already set.
# load_dotenv is idempotent and never overrides already-exported variables.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

from .clickhouse.connection import get_clickhouse_client  # noqa: E402,F401

logger = logging.getLogger("forex")


def initialize_clickhouse():
    """
    ClickHouse disabled — memory-only mode (fix later).
    Kept as a no-op so boot/import paths stay intact.
    """
    logger.info("ClickHouse disabled — running memory-only.")
    return None


# Trigger initialization — disabled: ClickHouse features removed for now.
# Kept guarded so re-enabling later is a one-line change.
try:
    initialize_clickhouse()
except Exception as e:
    logger.exception("ClickHouse initialization failed at boot: %s", e)
    print(
        "[ClickHouse] Initialization failed at boot — API continues; "
        "ClickHouse-dependent features will retry on demand."
    )
