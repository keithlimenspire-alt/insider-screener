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

# Phase 2 — judgement layer (strategy §4–§5)
# Role weights per §4: CFO / "Chief * Officer" / VP → high; General Counsel →
# high even solo; CEO → low (paid to be bullish); 10%-owner → lowest.
ROLE_WEIGHTS = {
    "GC": 1.0,
    "CFO": 0.9,
    "COO": 0.8, "CTO": 0.8, "CMO": 0.8, "CSO": 0.8, "CAO": 0.8, "CBO": 0.8,
    "C?O": 0.8,          # fallback for any other Chief * Officer
    "VP": 0.7,
    "Pres": 0.6,
    "Officer": 0.5,
    "Dir": 0.4,
    "CEO": 0.3,
    "Other": 0.2,
    "10%": 0.1,
}
CONVICTION_MIN_VALUE = 80_000        # §4: $80k–$500k+ = conviction
NOISE_BAND = (3_000, 15_000)         # §4: likely 401(k)/ESPP noise
FUND_NOISE_MAX_TRADE_PCT = 2.0       # §5: 10%-owner buying a tiny % of stake
STALE_AFTER_DAYS = 40                # §5: no follow-through for >5–6 weeks

# Phase 3 — context layer (strategy §3)
NEAR_HIGH_MAX_PCT_BELOW = 15.0       # §3: within 5–15% of multi-year high
ROUTINE_MIN_DISTINCT_MONTHS = 3      # §3: buys recurring ~monthly = routine
ROUTINE_LOOKBACK_DAYS = 365          # …counted in the year BEFORE the window
PRICE_HISTORY_YEARS = 3              # trailing window for the multi-year high
PRICE_CACHE_DIR = CACHE_DIR / "prices"
PRICE_CACHE_TTL_HOURS = 20.0

# V2 upgrade (strategy doc 20260719) — every threshold configurable for the
# eventual backtest sweep (V2 guardrail).
NOTABLE_TRADE_PCT = 10.0             # V2 §B: ~10% position increase = notable
REGIME_LOOKBACK_DAYS = 365           # V2 §B: sells-then-buy regime-flip window
REGIME_MIN_SELLS = 2                 # V2 §B: "a run of sells" = ≥N distinct sales
BELOW_MARKET_DISCOUNT_PCT = 5.0      # V2 §B: buy price ≥5% under market close
OFFERING_MIN_SAME_PRICE_UNITS = 3    # V2 §B: ≥N buying units at one identical
                                     # round price = offering/placement tell
                                     # (2 is too common for genuine clusters)
NEW_REPORTER_MONTHS = 12             # V2 §B: first insider filing younger than
                                     # this = likely FPI conversion
MA_GATE_DAYS = 50                    # V2 §B: 50-day MA entry gate (value branch)
MA_GATE_HOLD_DAYS = 3                # V2 §B: "reclaim and HOLD" = N closes above
BREADTH_WINDOW_DAYS = 20             # V2 §B: rolling window, unscheduled-buy share
BREADTH_BULLISH_PCT = 33.0           # V2 §B: above ~1/3 buys = bullish
BREADTH_VERY_BULLISH_PCT = 50.0      # V2 §B: historically very bullish
BREADTH_NEUTRAL_FLOOR_PCT = 25.0     # below this the gauge reads weak

# Tier scoring — composite cluster rank (S/A/B/C/D). Additive weights on the
# positive signals (counts capped so one dimension can't dominate), penalties
# for the §5 noise flags. Every number here is a backtest knob.
TIER_WEIGHTS = {
    "unit": 1.0,          # per independent buying unit
    "role": 1.5,          # × role score (GC/CFO-heavy clusters rank up)
    "conviction": 0.5,    # per ≥$80k buy
    "notable": 0.75,      # per unit growing its stake ≥10%
    "first_time": 0.5,    # per first-ever buyer
    "regime_flip": 1.0,   # per seller-turned-buyer
    "actionable": 1.0,    # timing gate passed (momentum, or value above 50d MA)
}
TIER_CAPS = {"unit": 5, "conviction": 5, "notable": 3, "first_time": 3,
             "regime_flip": 2}
TIER_PENALTIES = {
    "fund_noise": 3.0,    # mostly 10%-owner rebalancing money
    "routine": 2.0,       # insiders buy every month anyway
    "stale": 1.5,         # no follow-through >40d
    "all_noise_sized": 3.0,  # every buy in the 401(k)/ESPP band
    "new_reporter": 0.5,  # history signals unreliable (FPI conversion)
    "discounted": 2.0,    # majority of $ bought below market (a deal you can't get)
}
TIER_CUTOFFS = [("S", 10.0), ("A", 7.5), ("B", 5.0), ("C", 3.0)]  # below C → D
