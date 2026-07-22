"""Point-in-time DCF valuation from SEC XBRL company facts.

A deliberately mechanical, screener-grade DCF — not an analyst model:
    FCF base   = mean of the last (up to) 3 fiscal years of OCF − CapEx,
                 using only 10-K figures FILED by the as-of date
    growth     = historical FCF CAGR clamped to [0%, 15%], 5 years
    terminal   = 2.5% growth, discounted at a flat 10%
    equity     = PV(FCFs) + PV(terminal) − net debt
    fair/share = equity / shares outstanding
Undervaluation = 1 − price / fair. Returns None (no verdict) when FCF is
non-positive or data is missing — banks and pre-profit companies are simply
outside this model's competence, not "expensive".

Facts come from data.sec.gov/api/xbrl/companyfacts (cached; every fact
carries its filing date, so backtests only see numbers that were public).
"""
import json
import logging
from datetime import date

from . import config, edgar

log = logging.getLogger(__name__)

XBRL_DIR = config.CACHE_DIR / "xbrl"

OCF_TAGS = ("NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets")
DEBT_TAGS = ("LongTermDebtNoncurrent", "LongTermDebt", "DebtCurrent",
             "LongTermDebtCurrent")
CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")


def _facts(cik: str) -> dict | None:
    XBRL_DIR.mkdir(parents=True, exist_ok=True)
    path = XBRL_DIR / f"{cik}.json"
    if not path.exists():
        try:
            data = edgar.fetch(
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")
        except Exception as e:
            log.debug("companyfacts failed for %s: %s", cik, e)
            return None
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _annual(units: list, as_of: str, forms=("10-K", "10-K/A", "20-F")) -> dict[str, float]:
    """fiscal-year-end → value, using the latest figure FILED by as_of."""
    out: dict[str, tuple[str, float]] = {}
    for u in units:
        if u.get("form") not in forms or not u.get("end"):
            continue
        if (u.get("filed") or "9999") > as_of:
            continue
        # Annual figures span ~a year; monthly-ish periods are quarters.
        if u.get("start") and u["end"][:4] == u["start"][:4] and \
                int(u["end"][5:7]) - int(u["start"][5:7]) < 9:
            continue
        end, filed = u["end"], u.get("filed") or ""
        if end not in out or filed > out[end][0]:
            out[end] = (filed, float(u["val"]))
    return {k: v for k, (_, v) in out.items()}


def _instant(units: list, as_of: str) -> float | None:
    """Latest point-in-time (instant) value filed by as_of."""
    best = None
    for u in units:
        filed = u.get("filed") or ""
        if filed > as_of or u.get("val") is None:
            continue
        key = (u.get("end") or "", filed)
        if best is None or key > best[0]:
            best = (key, float(u["val"]))
    return best[1] if best else None


def _tag_units(facts: dict, taxonomy: str, tags: tuple) -> list:
    src = facts.get("facts", {}).get(taxonomy, {})
    for tag in tags:
        if tag in src:
            for unit_vals in src[tag].get("units", {}).values():
                return unit_vals
    return []


def fair_value_per_share(cik: str, as_of: date) -> float | None:
    """Screener-grade DCF fair value per share, or None when out of scope."""
    facts = _facts(cik)
    if not facts:
        return None
    cut = as_of.isoformat()
    ocf = _annual(_tag_units(facts, "us-gaap", OCF_TAGS), cut)
    capex = _annual(_tag_units(facts, "us-gaap", CAPEX_TAGS), cut)
    if len(ocf) < 2:
        return None
    years = sorted(ocf)[-3:]
    fcfs = [ocf[y] - capex.get(y, 0.0) for y in years]
    base = sum(fcfs) / len(fcfs)
    if base <= 0 or fcfs[-1] <= 0:
        return None
    if len(fcfs) >= 2 and fcfs[0] > 0:
        cagr = (fcfs[-1] / fcfs[0]) ** (1 / (len(fcfs) - 1)) - 1
    else:
        cagr = 0.0
    g1 = min(max(cagr, 0.0), config.DCF_GROWTH_CAP)
    r, gt = config.DCF_DISCOUNT_RATE, config.DCF_TERMINAL_G

    pv, f = 0.0, base
    for t in range(1, config.DCF_YEARS + 1):
        f *= (1 + g1)
        pv += f / (1 + r) ** t
    terminal = f * (1 + gt) / (r - gt)
    pv += terminal / (1 + r) ** config.DCF_YEARS

    # Long-term (noncurrent) + current portions; fall back to the combined
    # LongTermDebt tag only when the split isn't reported — never both.
    ltd = _instant(_tag_units(facts, "us-gaap", ("LongTermDebtNoncurrent",)), cut)
    if ltd is None:
        ltd = _instant(_tag_units(facts, "us-gaap", ("LongTermDebt",)), cut)
    cur = _instant(_tag_units(facts, "us-gaap",
                              ("DebtCurrent", "LongTermDebtCurrent")), cut)
    debt = (ltd or 0.0) + (cur or 0.0)
    cash = _instant(_tag_units(facts, "us-gaap", CASH_TAGS), cut) or 0.0
    shares = _instant(_tag_units(facts, "dei",
                                 ("EntityCommonStockSharesOutstanding",)), cut)
    if not shares or shares <= 0:
        return None
    equity = pv - debt + cash
    if equity <= 0:
        return None
    return equity / shares


def dcf_discount(cik: str, as_of: date, price: float | None) -> float | None:
    """Fractional undervaluation vs the DCF fair value (0.30 = 30% cheap);
    negative = trading above fair value. None = model not applicable."""
    if not price or price <= 0:
        return None
    fair = fair_value_per_share(cik, as_of)
    if fair is None or fair <= 0:
        return None
    return 1.0 - price / fair
