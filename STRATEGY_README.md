# Adaptive Paired Inventory Accumulation

Active strategy: accumulate complementary Polymarket UP (YES) / DOWN (NO) shares
on 5-minute crypto markets at a **combined acquisition cost ≤ 98¢**, then hold
matched pairs to settlement.

This is an **inventory pairing** strategy, not directional prediction.

## Rules

1. **Initial entry** when the **cheaper** side’s executable ask is ≤ **35¢**
   (`paired_initial_entry_direction=CHEAPER_SIDE`, configurable).
2. Buy that cheaper first leg (minimum **5** shares), then hedge the opposite side.
3. **Hedge** the opposite side only when  
   `opposite_ask ≤ 0.98 − unmatched_average_cost` (fees/slippage included).
4. Soft unmatched target: **5** shares. Adaptive DCA may exceed this only when
   economics improve and a credible hedge exists (hard cap **15**/side).
5. Operating phases by seconds left: EARLY → MIDDLE → LATE (configurable).
6. Never force a hedge above the profitability threshold merely to equalize shares.
7. Live trading requires `paired_live_trading_enabled=true` **and** `dry_run=false`.

## Defaults

| Knob | Default | Meaning |
|---|---|---|
| `paired_initial_entry_threshold` | `0.35` | Max ask for cheaper-side initial entry |
| `paired_initial_entry_direction` | `CHEAPER_SIDE` | First leg selection rule |
| `target_combined_pair_cost` | `0.98` | Max combined pair cost |
| `paired_max_inventory_per_side` | `15` | Hard inventory cap |
| `paired_unmatched_inventory_target` | `5` | Soft unmatched target |
| `paired_dca_increment` / `min_order_size` | `5` | Order size |
| `paired_max_dca_legs_per_side` | `3` | Max adaptive DCA legs |
| `paired_live_trading_enabled` | `false` | Explicit live switch |
| `dry_run` | `true` | Paper fills until validated |

## Setup

1. `pip install -r requirements.txt`
2. Configure `.env` locally (never commit secrets)
3. Keep `dry_run=true` and `paired_live_trading_enabled=false` until logs look sane
4. Deploy via `Procfile` (`uvicorn main:app`)

## Math

```
AVERAGE_COST = TOTAL_ACQUISITION_COST / TOTAL_SHARES
NEW_AVERAGE_COST = (EXISTING_COST + NEW_QTY * NEW_PX) / (EXISTING + NEW_QTY)
MAX_HEDGE = TARGET_COMBINED − UNMATCHED_AVERAGE_COST
PAIR_COST = AVERAGE_UP + AVERAGE_DOWN   # must be ≤ 0.98 after fees
```
