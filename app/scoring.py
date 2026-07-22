"""Composite tier scoring: rank clusters S / A / B / C / D.

Additive score over the judgement-layer signals (capped counts), minus
penalties for the noise flags. All knobs live in config so the backtest can
sweep them.
"""
import pandas as pd

from . import config


def tier_for(score: float) -> str:
    for name, cutoff in config.TIER_CUTOFFS:
        if score >= cutoff:
            return name
    return "D"


def score_clusters(cl: pd.DataFrame) -> pd.DataFrame:
    """Adds `score` and `tier` columns; returns the frame sorted by score.

    Works on the cluster frame from build_screen, optionally enriched with the
    market context (`actionable`, `below_market_value`) and the dashboard's
    `new_reporter` column — missing columns simply contribute nothing."""
    if cl.empty:
        return cl
    w, caps, pen = config.TIER_WEIGHTS, config.TIER_CAPS, config.TIER_PENALTIES

    def col(name, default=0):
        if name in cl:
            return pd.to_numeric(cl[name], errors="coerce").fillna(default)
        return pd.Series(default, index=cl.index, dtype=float)

    s = (w["unit"] * col("n_insiders").clip(upper=caps["unit"])
         + w["role"] * col("role_score")
         + w["conviction"] * col("n_conviction").clip(upper=caps["conviction"])
         + w["notable"] * col("n_notable").clip(upper=caps["notable"])
         + w["first_time"] * col("n_first_time").clip(upper=caps["first_time"])
         + w["regime_flip"] * col("n_regime_flip").clip(upper=caps["regime_flip"]))
    if "actionable" in cl:
        s = s + w["actionable"] * cl["actionable"].eq(True).astype(float)

    for flag, weight in (("fund_noise", pen["fund_noise"]),
                         ("routine", pen["routine"]),
                         ("stale", pen["stale"])):
        if flag in cl:
            s = s - weight * cl[flag].eq(True).astype(float)
    all_noise = (col("n_noise") >= col("n_buys")) & (col("n_noise") > 0)
    s = s - pen["all_noise_sized"] * all_noise.astype(float)
    if "below_market_value" in cl and "total_value" in cl:
        discounted = col("below_market_value") > 0.5 * col("total_value")
        s = s - pen["discounted"] * discounted.astype(float)
    if "new_reporter" in cl:
        s = s - pen["new_reporter"] * cl["new_reporter"].eq(True).astype(float)

    out = cl.copy()
    out["score"] = s.round(2)
    out["tier"] = out["score"].map(tier_for)
    # Margin-of-safety promotion: a cluster trading ≥DCF_UNDERVALUE_MIN below
    # its point-in-time DCF fair value is promoted to Tier S outright.
    if config.DCF_PROMOTE_TO_S and "dcf_discount" in out:
        cheap = pd.to_numeric(out["dcf_discount"], errors="coerce") \
            >= config.DCF_UNDERVALUE_MIN
        if cheap.any():
            out.loc[cheap, "tier"] = "S"
            if "flags" in out:
                out.loc[cheap, "flags"] = (out.loc[cheap, "flags"].fillna("")
                                           + ", dcf-value").str.strip(", ")
    return out.sort_values("score", ascending=False).reset_index(drop=True)
