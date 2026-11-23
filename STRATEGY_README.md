# High-confidence ask entry (hold to settle)

Active strategy: in the final entry window, if a Polymarket side is already at
or above the configured entry floor, buy that leading side and **hold to settlement**.

## Rules

1. Only evaluate in the configured entry window (default **final 60s**, with a
   minimum seconds-left cutoff).
2. Require best ask **≥ `entry_price`** (default **97¢**).
3. Buy the leading side (higher ask); YES wins ties.
4. Skip if the ask is above `max_price` (default **99¢**).
5. Execute **FOK** at the ask.
6. **Hold to settle** when `hold_to_settle=true` — otherwise stop-loss /
   take-profit exits apply (`stop_loss_pct` default **50%**).

## Defaults

| Knob | Default | Meaning |
|---|---|---|
| `entry_price` | `0.97` | Minimum ask to enter (97¢+) |
| `entry_window_seconds` | `60` | Final-window length |
| `min_entry_seconds_left` | `15` | Cutoff before expiry |
| `min_price` / `max_price` | `0.97` / `0.99` | Allowed ask band |
| `stop_loss_pct` | `0.50` | Stop loss when not holding to settle |
| `hold_to_settle` | `true` | No mid-trade exits |
| `listener_activate_secs` | `90` | WS/poll active before entry window |
| `dry_run` | `true` | Paper until validated |

## Setup

1. `pip install -r requirements.txt`
2. Configure `.env` locally (never commit secrets)
3. Keep dry-run on until `[ENTRY]` logs look sane
4. Deploy via `Procfile` (`uvicorn main:app`)
