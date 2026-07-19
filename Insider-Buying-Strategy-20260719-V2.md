# Insider-Buying Strategy — Working Ruleset

**Version:** V2 — 20260719. Supersedes V1 (20260713).
**Sources now folded in (tagged inline):**
- **[RG]** Ross Givens "Insider Trading Masterclass" (Parts 1–3) — near-highs / momentum lean.
- **[CW]** CEO Watcher masterclass (ceowatcher.com) — value/dip lean, market-breadth signal, 3-type framework.
- **[BUILT]** Your existing built system (May 2026 multi-pillar "coiled spring" insider pillar).
- **[ANALYSIS]** Findings from applying the ruleset to IPX (IperionX), 19 Jul.

> **Read this before touching code.** V2 is an *upgrade* to the already-built system, not a new build. The big change (§A) reconciles the contradiction that was blocking you — your built discount logic is **kept**, not replaced. The Claude Code change spec is in §10.
>
> Not financial advice. Every threshold below is a practitioner heuristic, none are backtested. Two sources now disagree in places — those disagreements are marked, and they're what a backtest is for.

---

## A. The fork is resolved — by classification, not by choosing [CW]

Your V1 blocker: **[RG]** says buy *near multi-year highs*; **[BUILT]** rewards buying *at a discount* below insider entry. CEO Watcher dissolves this — they're not competing rules, they're two different **trade types**. Classify the trade first, then apply the matching price rule:

| Trade type | What it looks like | Price rule | Source | Frequency |
|---|---|---|---|---|
| **Value** | Stock down hard from highs, low valuation vs its own history, insider betting on a turnaround | Reward discount-to-entry, long hold, no offsetting sells — **your existing coiled-spring logic** | [BUILT][CW] | Most common |
| **Momentum** | Stock near multi-year highs / uptrending, insider buying *into* strength | Reward proximity to highs + confirmation | [RG] | Rarer |
| **Catalyst** | Buy shortly before a known/expected event | Treat as a soft confirming signal only | [CW] | Rare |

**Consequence for the build:** the near-highs rule stops contradicting the discount rule because the scorer routes each name to a branch. Nothing gets deleted. The honest version of "which is right" becomes a *per-branch* backtest question — do value buys outperform? do momentum buys? — measured separately, not globally toggled.

---

## B. What's genuinely new in V2 (everything below, by lift)

### New market-wide signal — top-down [CW] ⚠️ new data required
Everything else here is bottom-up (pick a stock). This is a market timing gauge. Of *unscheduled* insider trades, ~⅓ are normally buys. When the **unscheduled-buy share crosses ~50%, historically very bullish for the whole market**; >33% bullish; well below, underperformance. (COVID ~60%, 2022 bottom ~55%, late-2018 ~54%.) This is a "should I be buying anything right now" overlay, not a stock picker. **It needs the full daily Form 4 firehose (~50–250 filings/day), which the built system does not currently ingest.** Biggest lift here — see §10.

### New entry gate — the 50-day rule [CW]
You had no timing logic. For **value** trades: don't catch a falling knife — wait for the stock to reclaim and *hold* its 50-day moving average before marking it actionable. Underneath is a philosophy split: **[CW]** says insiders are chronically *early* (Carvana — sold all the way up, bought the bottom), so don't blind-copy same-day. This directly contradicts **[RG]**, who says enter fast before the catalyst breaks. For **momentum** trades the gate is moot (price is already strong).

### Expanded low-signal exclusions [CW]
V1 only caught option exercises. Add these, all readable from the filing/footnotes — nuke before scoring:
- **10b5-1 scheduled trades** (plan checkbox or footnote)
- **Public offerings / private placements** — tells: suspiciously round price, shares paired with **warrants**, or **multiple insiders at the identical price**
- **Dividend reinvestments** and **tax sales**
- **Discounted / below-market purchases** — insider got a deal you can't; down-weight

### Scoring refinements [CW]
- **Sell-side track record** — not just "do their buys do well," but do their *sells* precede drops? Reward insiders who sell before falls and buy before rises.
- **Behaviour-change flag** — an insider flipping from a run of sells to a buy (or first-ever buy) is a strong tell. (V1 had first-time-buyer; this generalises it to regime change.)
- **Valuation confirmation** — after tagging a value buy, verify it's actually cheap on EV/EBITDA or P/E vs its own 5-year range. Don't trust "down a lot" as "cheap."
- **~10% position-increase** as the "notable" line for an individual.

### Two fixes found on IPX [ANALYSIS]
- **Trade-% carve-out — individuals vs institutions.** V1's low-trade-% filter was built to catch fund rebalancing (~0.5%). Applied literally it would reject a founder adding to a 26M-share stake (Hannigan's US$1.2M buy was only +2%). Carve out: apply the tiny-% reject **only to 10%-owners/institutions**, not to individuals.
- **FPI / Section 16 conversion flag.** If a company's **first Form 3 is < ~12 months old**, it likely just converted from foreign-private-issuer status — so there's *no insider history*. This silently breaks three checks: first-time-buyer (everyone's first by construction), track record (unrunnable), routine-vs-opportunistic (no baseline). Flag it; don't score those three; don't mistake a reporting-regime change for a conviction flood. (This is a false-signal *class* neither source names.)

---

## C. Carried over from V1 (unchanged, still in force)

Cluster ≥2 as the screen-in ticket (prefer 3+) · buying only, never selling · screen by dollar value not share count · role hierarchy (CFO/GC high, CEO low, 10%-owner lowest) · conviction size bands ($3–15k = 401(k)/ESPP noise, $80k–500k+ = real) · option-exercise trap (code M acquire + code S sale) · opportunistic-vs-routine · 48-hour reporting rule · 6-month short-swing hold · exit/position-sizing **still undefined** (the biggest hole — neither source fixes it).

---

## 10. Change spec for Claude Code (the built system)

**You are modifying an existing repo, not building from scratch. Do not rebuild. Do not delete the existing insider scoring.**

### Step 0 — read the repo first, then confirm these before changing anything
- Where does insider scoring live, and is it the full multi-pillar system or an insider-only build?
- Does the pipeline already store **price/technical history** (needed for 50-day MA + valuation)? The May spec implied yes (52-wk high/low, buy-zone technicals) — confirm.
- Does it ingest only per-ticker clusters, or the **full daily Form 4 index**? (Determines whether §B market-breadth is feasible or needs new ingestion.)
- Report the current insider data model back before writing code.

### Change set — grouped by lift, ship in this order

**Tier 1 — small, extend existing insider logic (do first):**
1. Expanded exclusion filters (10b5-1, offering/placement, DRIP, tax sale, discounted) — with the offering tells (round price, warrants, identical-price cluster).
2. Trade-% carve-out: tiny-% reject applies to 10%-owners/institutions only, not individuals.
3. Scoring refinements: sell-side track record, regime-flip flag, valuation confirmation vs 5-yr range, 10% notable threshold.
4. FPI flag: first-Form-3 < 12 months → suppress history-dependent sub-scores, surface a badge.

**Tier 2 — medium, new scoring branch:**
5. Implement the 3-type classifier (value / momentum / catalyst) from §A. **Route existing coiled-spring logic into the *value* branch unchanged.** Add the *momentum* branch (near-highs) as new. This is the fork fix — it should be a router in front of the price scoring, not a rewrite of it.
6. 50-day MA entry gate on the value branch → gates Tier promotion / "actionable" flag (not the score itself). Skip for momentum.

**Tier 3 — large, new module + new data (separate workstream, don't let it block Tiers 1–2):**
7. Market-wide breadth signal (§B). Requires ingesting the full daily Form 4 firehose, tagging scheduled vs unscheduled, computing the rolling unscheduled-buy %. Surface as a dashboard-level gauge/context banner that can modulate tier thresholds. Scope this on its own; it's the only item needing new ingestion.

### Guardrails
- Preserve the built discount/coiled-spring logic — it becomes the value branch, it is not wrong.
- Every new threshold goes in a config, not hardcoded — you'll want to sweep them in a backtest.
- Where [RG] and [CW] disagree (near-highs vs dip; fast entry vs 50-day gate), don't pick in code — implement both as branch-specific and leave the choice to the backtest.

### Kickoff prompt
> Read this V2 strategy doc, then read the existing insider dashboard repo and report back the current insider data model, whether price history is stored, and whether the full daily Form 4 index is ingested — before writing any code. Then implement Tier 1 of §10 only. Route nothing away from the existing coiled-spring logic yet; we add the classifier (Tier 2) after Tier 1 is verified. Flag anything in the spec that doesn't map cleanly onto what's already built.

---

## Changelog
- **V2 (20260719)** — Added CEO Watcher source. Resolved the highs-vs-discount fork via 3-type classification (value/momentum/catalyst) — existing discount logic retained as the value branch. Added: market-breadth signal, 50-day entry gate, expanded exclusion list, sell-side/regime/valuation refinements, trade-% individual-vs-institution carve-out, FPI/Section-16 flag. Reframed the whole doc as a change spec against the already-built system (§10).
- **V1 (20260713)** — Initial consolidation from the Givens 3-part transcript. Scan config, cluster logic, role weighting, red flags, option-exercise trap, workflow, examples, critique.
