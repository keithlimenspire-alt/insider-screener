"""Central configuration for the insider-buying screener."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "insider.db"
CACHE_DIR = DATA_DIR / "cache"
RAW_CACHE_DIR = CACHE_DIR / "raw"          # immutable accession .txt files
INDEX_CACHE_DIR = CACHE_DIR / "daily-index"

# SEC requires a User-Agent identifying you with a contact address,
# and caps automated traffic at 10 requests/second.
SEC_USER_AGENT = "Keith Lim keithlim.enspire@gmail.com"
MAX_REQUESTS_PER_SEC = 8.0                  # stay under the 10/s SEC limit
FETCH_WORKERS = 4                           # concurrent fetchers sharing the rate limiter

SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/"
DAILY_INDEX_BASE = "https://www.sec.gov/Archives/edgar/daily-index"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# Screener defaults (strategy §2)
DEFAULT_WINDOW_DAYS = 45
DEFAULT_MIN_BUY_VALUE = 200_000
DEFAULT_MIN_CLUSTER_SIZE = 2
