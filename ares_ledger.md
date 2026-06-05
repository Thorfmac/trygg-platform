# Trygg Ares — Decision & Outcome Ledger

**Purpose:** Every decision, deviation, and outcome through the paper-trading phase. The single source of truth for whether the system is calling things well enough to eventually trust with real capital.

**Cadence:** New rows added when the system surfaces a decision-relevant signal. Existing rows updated when (a) decision is made, (b) trade is placed in paper account, (c) catalyst resolves, (d) 30/60/90 day outcome reflection.

**Discipline:** A row is not "done" until the outcome field is filled. Empty outcome fields six months from now are the metric that says paper trading hasn't produced enough data to go live.

---

## How to read this ledger

Each entry has six lifecycle stages. Not every entry reaches every stage.

| Stage | What it means |
|---|---|
| **Signal** | The system flagged something. Briefing date + signal pool reference. |
| **Recommendation** | What the system said: Action / Watch / Review / WITHHOLD / Thesis intact. |
| **Decision** | What Thorfinn decided. If different from recommendation, **why** matters. |
| **Execution** | What was actually done in the paper account. Ticker, instrument, size, price. |
| **Resolution** | If the system issued a WITHHOLD, what the Tier 1 primary confirmed. |
| **Outcome** | How it played out. 30/60/90 day P&L, whether thesis held, what was learned. |

---

## Entries (newest first)

---

### Entry 04 — 01 Jun 2026 — LUNR LTV resolution (Tier 1 confirmed)

**Signal**
- Source: Briefing 31-May (signal pool ID pending — first Ares digest)
- Claim: NASA excluded LUNR from Lunar Terrain Vehicle program
- System recommendation: WITHHOLD pending Tier 1 confirmation

**Resolution (Tier 1 — confirmed spent, 01-Jun-2026)**
- NASA selected Lunar Outpost ($220M) and Astrolab ($219M) for LTV Phase 1 rover task orders
- Blue Origin awarded $188M (options to $280M) for rover delivery via Blue Moon Mark 1
- LUNR not selected for either rover work or delivery
- LTV Services framework continues through 2039 with phased task orders — future on-ramps possible
- LUNR retains dominant prime contractor position in CLPS (IM-3 Trinity on manifest)
- LUNR also won $20M lunar reconnaissance contracts (LRO, ShadowCam) — confirmed Tier 2

**Thesis impact**
- Surface infrastructure component of v5 thesis materially weakened
- CLPS-centric revenue component intact
- Q2 earnings (early Aug) is the first hard read on whether organic revenue grows without LTV
- v5 sizing (5-7% initial / 10% max, stop $25) assumed competitive participation in major NASA programs — LTV loss signals this is not guaranteed

**Decision (paper account)**
- [TO RECORD] Sizing chosen: ☐ Hold v5 as-is (5-7% initial / 10% max) ☐ Trim upper range (4-5% initial / 8% max) ☐ Wait for Q2 print, start at bottom of range
- Rationale: [TO RECORD]

**Execution**
- [TO RECORD] If/when placed: date, entry price, position size, stop level

**Outcome (30/60/90 day reflection)**
- 30 day: [TO COMPLETE]
- 60 day: [TO COMPLETE]
- 90 day: [TO COMPLETE]
- Q2 earnings result: [TO COMPLETE — early Aug 2026]

**System performance note**
- WITHHOLD discipline worked exactly as designed: system refused to assert load-bearing conclusion on Tier 3, named the specific Tier 1 primary, primary was fetched within hours, decision made with full context
- This is the loop the source-authority gate was built for

---

### Entry 03 — 01 Jun 2026 — ASTS Falcon 9 schedule (PENDING)

**Signal**
- Source: Briefing 01-Jun (signal IDs 22-23, 26)
- Claim: Blue Origin static-fire explosion 29-May materially delays ASTS BlueBird 8/9/10 mid-June launch
- System recommendation: WITHHOLD — gate caught Tier 3 inference (Falcon 9 schedule impact) resting on Tier 2 source that only confirmed the explosion itself

**Why this one matters**
- ASTS BlueBird launches on Falcon 9, not New Glenn. Blue Origin explosion does not mechanically delay a SpaceX manifest.
- Barchart (Tier 3) made the causal inference; system refused to inherit it
- v5 Tier 5 watchlist entry condition: ≤$90 AND confirmed BlueBird launch — only meaningful if launch is real

**Resolution (PENDING)**
- Required: SpaceX manifest update OR ASTS IR statement OR FAA launch licence status (Tier 1)
- Decision tree:
  - If launch on schedule → v5 re-entry trigger reactivated; watch price
  - If launch confirmed delayed → suspend re-entry watch for cycle one
  - If schedule officially unknown → maintain WITHHOLD, re-check weekly

**Decision (paper account)** — Pending resolution
**Execution** — Pending
**Outcome** — Pending

---

### Entry 02 — 01 Jun 2026 — IONQ Q1 revenue figures (PENDING)

**Signal**
- Source: Briefing 31-May (signal ID 8)
- Claim: IONQ Q1 2026 revenue $64.7M (+755% YoY), RPO $470M (+554%)
- System recommendation: WITHHOLD — Tier 3 (Seeking Alpha contributor) load-bearing for CSP sizing/strike decision

**Resolution (PENDING)**
- Required: IONQ Q1 2026 10-Q or 8-K on SEC EDGAR (Tier 1)
- Note from Opus 4.8 verification: numbers verified clean on Tier 1; system under-claimed its own source quality. If still unfiled on EDGAR, the IR page release would also be Tier 1.
- Decision tree:
  - If $64.7M / $470M confirmed → CSP thesis at $55-60 strengthens; consider tightening strike
  - If figures lower than reported → maintain v5 CSP framework, no change
  - If figures higher → consider raising CSP strike (collect more premium) or accepting lower-priced equity entry

**Decision (paper account)** — Pending resolution
**Execution** — Pending. Q2 earnings 12-Aug-2026 is the fundamental gate either way.
**Outcome** — Pending

---

### Entry 01 — 01 Jun 2026 — RKLB SDA Tranche 3 contract value (PENDING)

**Signal**
- Source: Briefing 31-May (signal IDs 18, 19)
- Claim: RKLB cleared SDA Tracking Layer Tranche 3 review; contract value $3.5B (Seeking Alpha) vs $1.3B+ (Benzinga)
- System recommendation: WITHHOLD — conflicting Tier 3 figures, automatic withhold per protocol

**Why this is less urgent**
- No current RKLB position (CSP entry condition $115-120 not triggered)
- Resolution affects future CSP sizing if entry condition is met
- Milestone passage itself (review clearance) is credible colour both sources agree on

**Resolution (PENDING)**
- Required: RKLB 8-K or press release stating contract value (Tier 1) OR SDA award notice (Tier 1)
- Decision tree:
  - If $3.5B confirmed → backlog thesis strengthens materially
  - If $1.3B+ confirmed → milestone is real but smaller revenue implication than higher figure
  - Either way, CSP entry framework at $115-120 unchanged unless RKLB pulls back

**Decision (paper account)** — No action required pending price trigger
**Execution** — N/A unless RKLB enters CSP zone
**Outcome** — Pending

---

## Position framework as of 01 Jun 2026 (universe doc v5)

Captured here so the ledger is self-contained. If v5 is amended, log the amendment as a new entry; do not edit this table.

| Ticker | Tier | Instrument | Initial sizing | Max sizing | Stop / Trigger |
|---|---|---|---|---|---|
| LUNR | 1 — Conviction | Equity | 5-7% | 10% (cap 12%) | Stop $25 |
| SATS | 1 — Conviction | Equity | 3% starter | 5-6% on AT&T close | Stop $60 |
| UFO | 1 — Conviction | ETF | 3% | 3% | No stop, monthly review |
| QTUM | 1 — Conviction | ETF | 2% | 2% | No stop, monthly review |
| IONQ | 2 — Defined-entry | Cash-secured put | $55-60 strike, 1.5-2% collateral | TBD on assignment | N/A pre-assignment |
| RKLB | 3 — Conditional | CSP on pullback | 2-3% collateral when $115-120 hit | TBD | N/A pre-trigger |
| QNT | 3 — Conditional | Post-IPO sequenced | TBD by listing price | TBD | TBD |
| SPCX | 4 — Catalyst trade | Long puts (Dec lockup) | 1-1.5% premium-at-risk | N/A | Time-bounded Dec 2026 |
| ASTS | 5 — Watchlist | Re-entry conditional | TBD on trigger | TBD | Trigger: ≤$90 AND confirmed Falcon 9 launch |

**Sizing rules (also v5):**
- Max single position: 10% (cap 12%)
- Max options premium aggregate: 4%
- Max single options position: 1%
- Max aggregate CSP collateral: 15%
- Sleeve drift: Space 60-75%, Quantum 15-25%, Cash 10-25%
- Drawdown triggers: -8% pause, -12% halve options, -20% written review with funder

---

## Open WITHHOLD queue (as of 01 Jun 2026)

| # | Ticker | Claim | Required primary | Priority | Status |
|---|---|---|---|---|---|
| ~~1~~ | ~~LUNR~~ | ~~NASA LTV exclusion~~ | ~~NASA award notice~~ | ~~High~~ | ✅ Resolved 01-Jun: confirmed spent, LUNR not on Phase 1 |
| 2 | ASTS | Falcon 9 BlueBird launch schedule | SpaceX manifest / ASTS IR | Medium | Pending |
| 3 | IONQ | Q1 2026 revenue $64.7M / RPO $470M | IONQ 10-Q or 8-K on EDGAR | Medium | Pending |
| 4 | RKLB | SDA Tranche 3 contract value ($3.5B vs $1.3B) | RKLB 8-K or SDA award notice | Low (no position) | Pending |

---

## Catalyst calendar (cycle one window)

Reproduced from v5 for ledger self-containment. Outcomes get filled in as catalysts pass.

| Date | Catalyst | Positions affected | Outcome |
|---|---|---|---|
| First week Jun 2026 | Quantinuum IPO listing | QNT, QTUM, IONQ | TBC |
| ~12 Jun 2026 | SpaceX IPO listing day | SATS, SPCX, UFO, RKLB, ASTS | TBC |
| Mid-Jun 2026 | ASTS BlueBird 8/9/10 Falcon 9 launch | ASTS | PENDING — see entry 03 |
| H1 2026 | SATS AT&T transaction close | SATS | TBC |
| Early Aug 2026 | LUNR Q2 2026 earnings | LUNR | TBC |
| 12 Aug 2026 | IONQ Q2 2026 earnings | IONQ | TBC |
| Q4 2026 (NET) | RKLB Neutron first launch | RKLB | TBC |
| 15-27 Dec 2026 | SPCX 180-day lockup expiry | SPCX | TBC |

---

## System performance log

Tracked separately from individual position outcomes. The metric that matters at the system level: was the system's recommendation right, even if Thorfinn's decision deviated?

| Date | Recommendation | Was it correct (with hindsight)? | What this teaches |
|---|---|---|---|
| 01-Jun | WITHHOLD on LUNR LTV claim | YES — Tier 3 inference about "thesis damage" was over-claiming; primary revealed nuance (loss is real, thesis less damaged than headline suggested) | Gate worked as designed |
| 01-Jun | WITHHOLD on ASTS Falcon 9 inference | TBD — pending resolution | Gate caught a causal inference the underlying Tier 2 source didn't make |
| 01-Jun | WITHHOLD on IONQ Q1 figures | LIKELY YES — Opus 4.8 verified numbers; system was conservative direction, which is benign | Slight over-tightness on gate; figures were real |
| 01-Jun | WITHHOLD on RKLB contract value | YES — conflicting Tier 3 figures genuinely cannot be acted on | Auto-withhold protocol working |

---

## Funder communication log

Track every funder conversation that materially affects the strategy. Empty until first conversation.

| Date | Topic | Outcome |
|---|---|---|
| TBC | Funder ratification mechanism agreed | Pending |
| TBC | Universe doc v5 approval | Pending |
| TBC | Paper-trading phase scope and duration | Pending |

---

## Lessons learned

Living section. Add a row every time the system or the operator does something worth remembering.

| Date | Lesson | Action taken / standing instruction |
|---|---|---|
| 31-May | Three load-bearing errors caught only by adversarial scrutiny (Mímir QNT valuation, v2 LUNR PT inversion, v4 SATS share-count) | Every universe doc revision and major cycle plan goes through adversarial scrutiny before becoming document of record |
| 31-May | Mímir 7-day Telegram silence caused by unawaited coroutine bug; same bug class can occur anywhere using python-telegram-bot | Ares uses direct-HTTP via aiohttp; consider porting same pattern to Mímir |
| 31-May | Two clients with same chat_id caused duplicate Telegram messages | Soft-delete via NULLed chat_id rather than row delete (FK constraints from historical digests) |
| 31-May | Schema INT[] vs UUID[] mismatch caused silent UPDATE failure on digest delivery audit | When designing schema, grep for "REFERENCES platform.*" before declaring any column type — platform.* uses UUIDs throughout |
| 01-Jun | Docker Desktop didn't auto-start after PC reboot; missed Mímir 07:00 UTC digest | Docker Desktop set to start at login; restart: unless-stopped policy verified on compose |
| 01-Jun | First Tier 1 primary fetched manually (LUNR LTV); resolution took ~5 minutes | EDGAR + NASA award page ingestion is the highest-value next system build — closes the manual loop |

---

*Last updated: 01 Jun 2026*
*Canonical version: this markdown file. SQL audit version: `ares.ledger` table in Postgres.*
