# Polymarket Whale Tracker GUI Context

This document provides NotebookLM with a compact but explicit representation of the GUI layout, interaction flow, and displayed data so it can reason about the interface even without direct UI files or screenshots.

---

## 1) GUI Purpose

The GUI is a Streamlit dashboard for monitoring:

- live Polymarket spike events,
- research signal assessments (deterministic + optional LLM probability),
- cross-asset consequence predictions,
- daily top spikes ranked by research signal probability.

It is designed for traders and technical users who need rapid triage of market-moving anomalies.

---

## 2) High-Level Layout

Top of page:

- Title: `Polymarket Whale Tracker`
- Subtitle: live anomaly feed context

Left sidebar controls:

- API base URL input
- news dataset path input
- recent spikes limit slider
- auto-refresh seconds slider
- manual refresh button

Main body tabs:

- `Live Feed`
- `Daily Top Signals`

---

## 3) Visual Wireframe (Approximate)

```text
+----------------------------------------------------------------------------------+
| Polymarket Whale Tracker                                                         |
| Live public-data anomaly feed with the newest event pinned at the top           |
+----------------------------------------------------------------------------------+
| Sidebar (left)                | Main Content (right)                             |
| - API base URL                | [Tab: Live Feed] [Tab: Daily Top Signals]        |
| - News dataset path           |                                                   |
| - Recent spikes slider        | Live Feed tab:                                    |
| - Auto-refresh slider         |   - Live Latest Spike card                        |
| - Refresh button              |   - Older Whale Spikes cards                      |
|                               |                                                   |
|                               | Daily Top Signals tab:                            |
|                               |   - Ranked table for today's top probabilities    |
|                               |   - Click row -> Selected Event Full View         |
+----------------------------------------------------------------------------------+
```

---

## 4) Live Feed Tab: Content Blocks

### A) Live Latest Spike

Expanded event card with:

- Event ID
- YES / NO prices
- Research Signal Assessment section:
  - research signal probability (if LLM assessment exists),
  - explanation confidence,
  - summary text,
  - or deterministic-only fallback messaging.
- Market Summary section:
  - Event title
  - Market title/question
  - Category
  - Volume (compact K/M/B format)
  - Liquidity (compact K/M/B format)
  - `Open on Polymarket` link (using event slug)
- Latest Spike Details:
  - side, from price, to price, relative move, timestamp
- Cross-Asset Consequence Alerts:
  - asset, class, horizon, direction, magnitude, confidence, score, signal_time
  - confidence color coding:
    - red: < 50%
    - orange: 50-65%
    - green: >= 65%
- Event Spike History (expandable table)

### B) Older Whale Spikes

Same event-card structure repeated for additional recent events.

---

## 5) Daily Top Signals Tab: Content Blocks

### A) Daily Ranking Table

- Header indicates UTC day.
- Rows are today's spikes with non-null research signal probability.
- Sorted by:
  1) probability descending,
  2) signal time descending.

Columns include:

- event (name + event_id),
- signal time,
- research signal probability,
- explanation confidence,
- deterministic score,
- score band,
- side,
- price move,
- summary.

### B) Click-to-Open Event Detail

- User clicks a row in the daily table.
- Dashboard loads the selected event's full card below:
  - same complete detail view as Live Feed event cards.

---

## 6) Empty-State Logic

No daily data:

- message indicates no assessed spikes with probability stored for current UTC day.

No cross-asset prediction for an event:

- message explains likely reasons:
  - limited direct impact on tradable assets, or
  - AI validation filtered weak/non-specific outputs.

---

## 7) Data Provenance Behind UI

The GUI pulls from:

- `whale_spikes` table (raw spike rows),
- `insider_assessments` table (deterministic + research probability),
- `cross_asset_predictions` table (asset-level consequences),
- live proxy API for event metadata and current prices.

---

## 8) User Journey (Trader Perspective)

1. Open Live Feed to monitor newest anomalies in real time.
2. Inspect research signal probability and confidence.
3. Review cross-asset consequences when available.
4. Jump to Polymarket event page from card link.
5. Use Daily Top Signals tab for a probability-ranked shortlist.
6. Click a ranked row to open full event detail and decide action.

---

## 9) NotebookLM Interpretation Notes

When generating narration or summaries from this GUI:

- treat this interface as a real-time signal triage dashboard,
- emphasize confidence/ranking and click-through workflows,
- mention both direct Polymarket action and cross-asset automation pathways.
