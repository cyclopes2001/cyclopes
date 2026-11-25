"""
Centralized configuration: trading_config.json workers + .env secrets/globals.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

SUPPORTED_TRADING_ASSETS: frozenset[str] = frozenset(
    {"btc", "eth", "sol", "xrp", "doge", "hype", "bnb"}
)
SUPPORTED_WINDOWS: frozenset[str] = frozenset({"5m", "15m"})
WINDOW_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}
MIN_SHARES: int = 5

_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "btc",
    "ethereum": "eth",
    "solana": "sol",
    "ripple": "xrp",
}


def _fatal(message: str) -> None:
    print(f"❌ [config] {message}", file=sys.stderr)
    sys.exit(1)


def normalize_asset_slug(raw: str) -> str:
    token = (raw or "").strip().lower()
    if not token:
        raise ValueError("empty asset token")
    return _ASSET_ALIASES.get(token, token)


def normalize_window(raw: str) -> str:
    w = (raw or "").strip().lower()
    if w not in SUPPORTED_WINDOWS:
        raise ValueError(f"unsupported window {raw!r}")
    return w


def worker_key(asset: str, window: str) -> str:
    return f"{normalize_asset_slug(asset)}:{normalize_window(window)}"


def _parse_unit_fraction(name: str, value: Any, default: float) -> float:
    """Parse a config fraction in (0, 1] — used for entry threshold and stop-loss pct."""
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v <= 0 or v > 1 or v != v:
        _fatal(f"{name} must be in (0, 1] (got {raw!r}).")
    return v


def _parse_positive_int(name: str, value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        _fatal(f"{name}={value!r} is not a valid integer.")
    if v < MIN_SHARES:
        _fatal(f"{name} must be >= {MIN_SHARES} (got {value!r}).")
    return v


def _parse_order_size(name: str, value: Any, default: float) -> float:
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v < MIN_SHARES or v != v or v in (float("inf"), float("-inf")):
        _fatal(f"{name} must be >= {MIN_SHARES} (got {raw!r}).")
    return v


def _parse_max_shares(name: str, value: Any, default: float) -> float:
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v < MIN_SHARES or v != v or v in (float("inf"), float("-inf")):
        _fatal(f"{name} must be >= {MIN_SHARES} (got {raw!r}).")
    return v


def _parse_nonnegative_float(name: str, value: Any, default: float) -> float:
    raw = value if value is not None else default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fatal(f"{name}={raw!r} is not a valid number.")
    if v < 0 or v != v or v in (float("inf"), float("-inf")):
        _fatal(f"{name} must be >= 0 (got {raw!r}).")
    return v


def _parse_bool_value(name: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_cooldown_ms(name: str, value: Any, default: int) -> int:
    try:
        v = int(value if value is not None else default)
    except (TypeError, ValueError):
        _fatal(f"{name}={value!r} is not a valid integer.")
    if v < 0:
        _fatal(f"{name} must be >= 0 (got {value!r}).")
    return v


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _load_env_sizing_overrides() -> Optional[dict[str, float]]:
    """Apply sizing overrides only when ORDER_SIZE_MIN, ORDER_SIZE_MAX, and MAX_SHARES are all set."""
    keys = ("ORDER_SIZE_MIN", "ORDER_SIZE_MAX", "MAX_SHARES")
    raw = {k: os.getenv(k, "").strip() for k in keys}
    set_keys = [k for k, v in raw.items() if v]
    if not set_keys:
        return None
    if len(set_keys) != len(keys):
        _fatal(
            "ORDER_SIZE_MIN, ORDER_SIZE_MAX, and MAX_SHARES must all be set together "
            f"to override sizing (found: {', '.join(set_keys)}). "
            "Omit all three to use trading_config.json defaults."
        )
    out: dict[str, float] = {
        "order_size_min": _parse_order_size("ORDER_SIZE_MIN", raw["ORDER_SIZE_MIN"], 0.0),
        "order_size_max": _parse_order_size("ORDER_SIZE_MAX", raw["ORDER_SIZE_MAX"], 0.0),
        "max_shares": _parse_max_shares("MAX_SHARES", raw["MAX_SHARES"], 0.0),
    }
    optional_max_order = os.getenv("MAX_ORDER_SIZE", "").strip()
    if optional_max_order:
        out["max_order_size"] = _parse_order_size("MAX_ORDER_SIZE", optional_max_order, 0.0)
    return out


ENV_SIZING_OVERRIDES: Optional[dict[str, float]] = _load_env_sizing_overrides()


DRY_RUN_DEFAULT: bool = _parse_bool_env("DRY_RUN_DEFAULT", _parse_bool_env("DRY_MODE", True))


def _cfg_get(raw: dict, defaults: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present value from worker entry or defaults."""
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
        if key in defaults and defaults[key] is not None:
            return defaults[key]
    return default


@dataclass(frozen=True)
class WorkerConfig:
    asset: str
    window: str
    # Legacy fields kept for dashboard/backward compatibility. Gabagool does not use them for entries/exits.
    momentum_entry_threshold: float = 0.90
    stop_loss_pct: float = 0.50
    take_profit_pct: float = 0.50
    price_99_entry_window_seconds: int = 90
    inventory_accumulation_enabled: bool = True
    cheap_side_min_price: float = 0.05
    cheap_side_max_price: float = 0.35
    target_combined_pair_cost: float = 0.98
    max_combined_pair_cost: float = 0.98
    # Adaptive paired inventory accumulation
    paired_initial_entry_threshold: float = 0.64
    paired_initial_entry_direction: str = "EXPENSIVE_SIDE"
    paired_max_inventory_per_side: float = 15.0
    paired_unmatched_inventory_target: float = 5.0
    paired_dca_increment: float = 5.0
    paired_max_dca_legs_per_side: int = 3
    paired_allow_adaptive_dca: bool = True
    paired_enable_dynamic_hedge_pricing: bool = True
    paired_enable_adaptive_hedge_timeout: bool = True
    paired_enable_adaptive_risk: bool = True
    paired_live_trading_enabled: bool = False
    paired_early_min_seconds_left: float = 180.0
    paired_middle_min_seconds_left: float = 60.0
    paired_stop_new_accumulation_seconds: float = 90.0
    paired_force_exit_eval_seconds: float = 45.0
    paired_exit_max_loss_fraction: float = 0.35
    strategy_mode: str = "paired_inventory_accumulation"
    max_entries_per_side_per_market: int = 1
    no_buys_last_minutes: int = 2
    cheap_entry_price: float = 0.20
    initial_hedge_delay_seconds: int = 0
    combined_cost_target: float = 0.98
    max_combined_cost: float = 0.98
    entry_window_seconds: int = 0
    max_entry_price: float = 0.20
    order_type: str = "FOK"
    max_slippage: float = 0.005
    max_quote_age_seconds: float = 3.0
    max_hedge_retries: int = 3
    hedge_retry_delay_ms: int = 400
    max_market_exposure: float = 40.0
    max_position_size: float = 20.0
    max_unmatched_yes: float = 20.0
    max_unmatched_no: float = 20.0
    max_share_imbalance: float = 0.0
    max_unmatched_shares: float = 0.0
    balance_inventory_before_buy: bool = True
    account_for_pending_buy_orders: bool = True
    allow_unmatched_accumulation: bool = False
    min_order_size: float = float(MIN_SHARES)
    late_entry_enabled: bool = False
    entry_price: float = 0.97
    entry_price_threshold: float = 0.95
    late_entry_max_slippage: float = 0.005
    min_time_before_close_seconds: int = 0
    stop_loss_exit_max_retries: int = 5
    stop_loss_exit_retry_ms: int = 400
    gabagool_initial_entry_threshold: float = 0.90
    gabagool_max_pair_cost: float = 0.98
    gabagool_min_profit_margin: float = 0.02
    gabagool_max_unpaired_shares: float = 5.0
    gabagool_min_time_to_resolution: int = 60
    gabagool_price_buffer: float = 0.00
    gabagool_inventory_soft_limit: float = 2.0
    gabagool_endgame_seconds: int = 30
    gabagool_endgame_max_unpaired: float = 1.0
    gabagool_falling_knife_enabled: bool = True
    gabagool_falling_knife_window_seconds: float = 3.0
    gabagool_falling_knife_min_drop: float = 0.035
    gabagool_falling_knife_fast_window_seconds: float = 1.0
    gabagool_falling_knife_fast_drop: float = 0.015
    gabagool_falling_knife_recovery_cents: float = 1.5
    gabagool_falling_knife_cooldown_seconds: float = 5.0

    # Arbigab-style three-engine architecture
    momentum_engine_enabled: bool = True
    momentum_min_delta: float = 0.0015
    momentum_signal_max_age: float = 0.75
    momentum_execution_mode: str = "single_taker"
    momentum_max_position_delta: float = 5.0
    market_making_enabled: bool = True
    market_making_quote_size: float = 5.0
    market_making_min_spread: float = 0.01
    market_making_quote_offset: float = 0.01
    market_making_max_position_delta: float = 5.0
    market_making_requote_seconds: float = 0.40
    spread_capture_enabled: bool = True
    spread_capture_trigger_sum: float = 0.995
    spread_capture_max_entry_sum: float = 0.985
    spread_capture_max_unpaired: float = 1.0
    spread_capture_min_net_profit: float = 0.005
    spread_capture_fee_rate: float = 0.0
    spread_capture_slippage_buffer: float = 0.002
    spread_capture_min_book_liquidity: float = 5.0
    spread_capture_max_data_age_seconds: float = 3.0
    spread_capture_second_leg_timeout_ms: int = 10000
    spread_capture_max_recovery_loss: float = 2.0
    spread_capture_release_blocked_to_eligible: bool = False
    inventory_max_pair_cost: float = 0.98
    inventory_min_net_edge: float = 0.02
    inventory_max_capital_per_market: float = 40.0
    inventory_max_dca_attempts: int = 8
    inventory_dca_cooldown_seconds: float = 60.0
    inventory_dca_min_price_move: float = 0.03
    inventory_max_imbalance_seconds: float = 7200.0
    inventory_max_active_orders: int = 4
    min_net_profit_per_pair: float = 0.02
    force_hedge_after_timeout: bool = False
    max_consecutive_buys_per_outcome: int = 3
    stabilization_window_seconds: float = 10.0
    stabilization_tolerance_percent: float = 0.20
    short_window_seconds: float = 5.0
    max_short_window_move_percent: float = 0.15
    latency_arb_enabled: bool = True
    # Legacy mid-window momentum knobs (unused by high-confidence entry)
    lookback_secs: float = 2.0
    entry_min_delta: float = 0.01  # percent units (0.01 = 0.01%)
    eval_interval_ms: int = 1000
    entry_delay_secs: float = 3.0
    latency_arb_min_ask: float = 0.97  # min_price / entry floor
    latency_arb_max_ask: float = 0.99  # max_price
    latency_arb_signal_max_age_seconds: float = 0.75
    # Late-window hold-to-settle mode
    endgame_enabled: bool = True
    hold_to_settle: bool = True
    endgame_max_seconds_left: float = 60.0
    endgame_min_seconds_left: float = 15.0
    endgame_min_move_bps: float = 10.0
    min_orderbook_depth: float = 5.0
    falling_knife_filter_enabled: bool = True
    price_drop_window_seconds: float = 15.0
    max_price_drop_percent: float = 3.5
    falling_knife_cooldown_seconds: float = 5.0
    trade_cooldown_ms: int = 1000
    order_size_min: float = 5.0
    order_size_max: float = 5.0
    max_order_size: float = 5.0
    max_shares: float = 20.0
    dry_run: bool = DRY_RUN_DEFAULT
    dry_run_fill_delay_min_ms: int = 200
    dry_run_fill_delay_max_ms: int = 2500
    listener_activate_secs: int = 95
    entry_seconds_left: int = 90
    min_entry_seconds_left: int = 0
    fill_timeout_ms: int = 10000
    fill_poll_ms: int = 400
    enabled: bool = True

    @property
    def interval_seconds(self) -> int:
        return WINDOW_SECONDS[self.window]

    @property
    def key(self) -> str:
        return worker_key(self.asset, self.window)

    def market_slug(self, start_ts: int) -> str:
        return f"{self.asset}-updown-{self.window}-{start_ts}"

    @property
    def order_size(self) -> float:
        return self.order_size_max

    @property
    def random_order_size(self) -> bool:
        return self.order_size_min < self.order_size_max - 1e-9


def _merge_worker_entry(raw: dict, defaults: dict) -> WorkerConfig:
    asset = normalize_asset_slug(str(raw.get("asset", "")))
    if asset not in SUPPORTED_TRADING_ASSETS:
        _fatal(f"Invalid asset {raw.get('asset')!r}. Supported: {sorted(SUPPORTED_TRADING_ASSETS)}")

    try:
        window = normalize_window(str(raw.get("window", "")))
    except ValueError:
        _fatal(
            f"Invalid window {raw.get('window')!r} for {asset}. "
            f"Supported: {sorted(SUPPORTED_WINDOWS)}"
        )

    momentum_entry_threshold = _parse_unit_fraction(
        "momentum_entry_threshold",
        _cfg_get(raw, defaults, "momentum_entry_threshold"),
        float(defaults.get("momentum_entry_threshold", 0.90)),
    )
    env_momentum = os.getenv("MOMENTUM_ENTRY_THRESHOLD", "").strip()
    if env_momentum:
        momentum_entry_threshold = _parse_unit_fraction(
            "MOMENTUM_ENTRY_THRESHOLD", env_momentum, momentum_entry_threshold,
        )
    stop_loss_raw = _cfg_get(raw, defaults, "stop_loss_pct", default=0.50)
    env_stop = os.getenv("STOP_LOSS_PERCENT", os.getenv("STOP_LOSS_PCT", "")).strip()
    if env_stop:
        stop_loss_raw = env_stop
    try:
        stop_loss_pct = float(stop_loss_raw)
    except (TypeError, ValueError):
        _fatal(f"stop_loss_pct={stop_loss_raw!r} is not a valid number")
    if stop_loss_pct > 1.0:
        stop_loss_pct /= 100.0
    if stop_loss_pct <= 0 or stop_loss_pct >= 1 or stop_loss_pct != stop_loss_pct:
        _fatal(f"stop_loss_pct must be in (0, 1) (got {stop_loss_raw!r})")

    take_profit_raw = _cfg_get(raw, defaults, "take_profit_pct", default=0.50)
    env_tp = os.getenv("TAKE_PROFIT_PERCENT", os.getenv("TAKE_PROFIT_PCT", "")).strip()
    if env_tp:
        take_profit_raw = env_tp
    try:
        take_profit_pct = float(take_profit_raw)
    except (TypeError, ValueError):
        _fatal(f"take_profit_pct={take_profit_raw!r} is not a valid number")
    if take_profit_pct > 1.0:
        take_profit_pct /= 100.0
    if take_profit_pct <= 0 or take_profit_pct != take_profit_pct:
        _fatal(f"take_profit_pct must be positive (got {take_profit_raw!r})")

    entry_price_raw = _cfg_get(
        raw, defaults, "entry_price", "cheap_side_max_price", "max_entry_price",
        default=0.97,
    )
    env_entry_price = os.getenv("ENTRY_PRICE", os.getenv("MAX_ENTRY_PRICE", "")).strip()
    if env_entry_price:
        entry_price_raw = env_entry_price
    try:
        entry_price = float(entry_price_raw)
    except (TypeError, ValueError):
        _fatal(f"entry_price={entry_price_raw!r} is not a valid number")
    if entry_price > 1.0:
        entry_price /= 100.0
    if entry_price <= 0 or entry_price >= 1 or entry_price != entry_price:
        _fatal(f"entry_price must be in (0, 1) (got {entry_price_raw!r})")
    from utils.clob_helpers import clamp_buy_price

    entry_price = clamp_buy_price(entry_price)

    entry_window_raw = _cfg_get(
        raw, defaults, "entry_window_seconds", "price_99_entry_window_seconds", default=0,
    )
    env_entry_window = os.getenv("ENTRY_WINDOW_SECONDS", "").strip()
    if not env_entry_window:
        env_entry_window = os.getenv("PRICE_99_ENTRY_WINDOW_SECONDS", "").strip()
    if env_entry_window:
        entry_window_raw = env_entry_window
    try:
        entry_window_seconds = int(entry_window_raw)
        price_99_entry_window_seconds = entry_window_seconds
    except (TypeError, ValueError):
        _fatal(f"entry_window_seconds={entry_window_raw!r} is not a valid integer")
    if entry_window_seconds < 0 or entry_window_seconds > 900:
        _fatal("entry_window_seconds must be between 0 and 900 (0 = no window limit)")

    cheap_min_raw = _cfg_get(raw, defaults, "cheap_side_min_price", default=0.10)
    env_cheap_min = os.getenv("CHEAP_SIDE_MIN_PRICE", "").strip()
    if env_cheap_min:
        cheap_min_raw = env_cheap_min
    cheap_side_min_price = _parse_unit_fraction("cheap_side_min_price", cheap_min_raw, 0.10)

    cheap_max_raw = _cfg_get(raw, defaults, "cheap_side_max_price", default=0.20)
    env_cheap_max = os.getenv("CHEAP_SIDE_MAX_PRICE", "").strip()
    if env_cheap_max:
        cheap_max_raw = env_cheap_max
    cheap_side_max_price = _parse_unit_fraction("cheap_side_max_price", cheap_max_raw, 0.20)
    if cheap_side_min_price > cheap_side_max_price + 1e-9:
        _fatal(
            f"{asset}:{window}: cheap_side_min_price={cheap_side_min_price} "
            f"cannot exceed cheap_side_max_price={cheap_side_max_price}"
        )

    combined_target_raw = _cfg_get(
        raw, defaults, "target_combined_pair_cost", "combined_cost_target", default=0.98,
    )
    env_target = os.getenv("TARGET_COMBINED_PAIR_COST", "").strip()
    if not env_target:
        env_target = os.getenv("COMBINED_COST_TARGET", "").strip()
    if env_target:
        combined_target_raw = env_target
    target_combined_pair_cost = _parse_unit_fraction(
        "target_combined_pair_cost", combined_target_raw, 0.98,
    )
    combined_cost_target = target_combined_pair_cost

    max_combined_raw = _cfg_get(
        raw, defaults, "max_combined_pair_cost", "max_combined_cost", default=0.98,
    )
    env_max_combined = os.getenv("MAX_COMBINED_PAIR_COST", "").strip()
    if not env_max_combined:
        env_max_combined = os.getenv("MAX_COMBINED_COST", "").strip()
    if env_max_combined:
        max_combined_raw = env_max_combined
    max_combined_pair_cost = _parse_unit_fraction("max_combined_pair_cost", max_combined_raw, 0.98)
    max_combined_cost = max_combined_pair_cost
    if combined_cost_target > max_combined_cost + 1e-9:
        _fatal(
            f"{asset}:{window}: target_combined_pair_cost={combined_cost_target} "
            f"cannot exceed max_combined_pair_cost={max_combined_cost}"
        )

    cheap_entry_price = cheap_side_max_price
    max_entry_price = cheap_side_max_price

    max_entries_raw = _cfg_get(raw, defaults, "max_entries_per_side_per_market", default=1)
    try:
        max_entries_per_side_per_market = int(max_entries_raw)
    except (TypeError, ValueError):
        _fatal(f"max_entries_per_side_per_market={max_entries_raw!r} is not a valid integer")
    if max_entries_per_side_per_market < 1:
        _fatal(f"{asset}:{window}: max_entries_per_side_per_market must be >= 1")

    no_buys_raw = _cfg_get(raw, defaults, "no_buys_last_minutes", default=2)
    env_no_buys = os.getenv("NO_BUYS_LAST_MINUTES", "").strip()
    if env_no_buys:
        no_buys_raw = env_no_buys
    try:
        no_buys_last_minutes = int(no_buys_raw)
    except (TypeError, ValueError):
        _fatal(f"no_buys_last_minutes={no_buys_raw!r} is not a valid integer")
    if no_buys_last_minutes < 0:
        _fatal(f"{asset}:{window}: no_buys_last_minutes must be >= 0")

    hedge_delay_raw = _cfg_get(raw, defaults, "initial_hedge_delay_seconds", default=0)
    env_hedge_delay = os.getenv("INITIAL_HEDGE_DELAY_SECONDS", "").strip()
    if env_hedge_delay:
        hedge_delay_raw = env_hedge_delay
    try:
        initial_hedge_delay_seconds = int(hedge_delay_raw)
    except (TypeError, ValueError):
        _fatal(f"initial_hedge_delay_seconds={hedge_delay_raw!r} is not a valid integer")
    if initial_hedge_delay_seconds < 0:
        _fatal(f"{asset}:{window}: initial_hedge_delay_seconds must be >= 0")

    order_type = str(
        os.getenv("ORDER_TYPE", _cfg_get(raw, defaults, "order_type", default="FOK"))
    ).strip().upper()
    if order_type != "FOK":
        _fatal(f"ORDER_TYPE must be FOK (got {order_type!r})")

    inventory_accumulation_enabled = _parse_bool_value(
        "inventory_accumulation_enabled",
        _cfg_get(raw, defaults, "inventory_accumulation_enabled", default=True),
        True,
    )
    paired_initial_entry_threshold = _parse_unit_fraction(
        "paired_initial_entry_threshold",
        _cfg_get(
            raw,
            defaults,
            "paired_initial_entry_threshold",
            "initial_entry_min_price",
            default=0.64,
        ),
        0.64,
    )
    paired_initial_entry_direction = str(
        _cfg_get(
            raw,
            defaults,
            "paired_initial_entry_direction",
            "initial_entry_direction",
            default="EXPENSIVE_SIDE",
        )
    ).strip().upper()
    if paired_initial_entry_direction not in ("EXPENSIVE_SIDE", "CHEAP_SIDE"):
        _fatal(
            f"{asset}:{window}: paired_initial_entry_direction must be "
            f"EXPENSIVE_SIDE or CHEAP_SIDE (got {paired_initial_entry_direction!r})"
        )
    paired_max_inventory_per_side = _parse_max_shares(
        "paired_max_inventory_per_side",
        _cfg_get(raw, defaults, "paired_max_inventory_per_side", "max_shares", default=15.0),
        15.0,
    )
    paired_unmatched_inventory_target = _parse_nonnegative_float(
        "paired_unmatched_inventory_target",
        _cfg_get(raw, defaults, "paired_unmatched_inventory_target", default=5.0),
        5.0,
    )
    paired_dca_increment = _parse_order_size(
        "paired_dca_increment",
        _cfg_get(raw, defaults, "paired_dca_increment", "order_size_max", default=5.0),
        5.0,
    )
    paired_max_dca_legs_raw = _cfg_get(raw, defaults, "paired_max_dca_legs_per_side", default=3)
    try:
        paired_max_dca_legs_per_side = int(paired_max_dca_legs_raw)
    except (TypeError, ValueError):
        _fatal(f"paired_max_dca_legs_per_side={paired_max_dca_legs_raw!r} is not a valid integer")
    if paired_max_dca_legs_per_side < 0:
        _fatal(f"{asset}:{window}: paired_max_dca_legs_per_side must be >= 0")
    paired_allow_adaptive_dca = _parse_bool_value(
        "paired_allow_adaptive_dca",
        _cfg_get(raw, defaults, "paired_allow_adaptive_dca", default=True),
        True,
    )
    paired_enable_dynamic_hedge_pricing = _parse_bool_value(
        "paired_enable_dynamic_hedge_pricing",
        _cfg_get(raw, defaults, "paired_enable_dynamic_hedge_pricing", default=True),
        True,
    )
    paired_enable_adaptive_hedge_timeout = _parse_bool_value(
        "paired_enable_adaptive_hedge_timeout",
        _cfg_get(raw, defaults, "paired_enable_adaptive_hedge_timeout", default=True),
        True,
    )
    paired_enable_adaptive_risk = _parse_bool_value(
        "paired_enable_adaptive_risk",
        _cfg_get(raw, defaults, "paired_enable_adaptive_risk", default=True),
        True,
    )
    paired_live_trading_enabled = _parse_bool_value(
        "paired_live_trading_enabled",
        _cfg_get(raw, defaults, "paired_live_trading_enabled", default=False),
        False,
    )
    paired_early_min_seconds_left = _parse_nonnegative_float(
        "paired_early_min_seconds_left",
        _cfg_get(raw, defaults, "paired_early_min_seconds_left", default=180.0),
        180.0,
    )
    paired_middle_min_seconds_left = _parse_nonnegative_float(
        "paired_middle_min_seconds_left",
        _cfg_get(raw, defaults, "paired_middle_min_seconds_left", default=60.0),
        60.0,
    )
    paired_stop_new_accumulation_seconds = _parse_nonnegative_float(
        "paired_stop_new_accumulation_seconds",
        _cfg_get(raw, defaults, "paired_stop_new_accumulation_seconds", default=90.0),
        90.0,
    )
    paired_force_exit_eval_seconds = _parse_nonnegative_float(
        "paired_force_exit_eval_seconds",
        _cfg_get(raw, defaults, "paired_force_exit_eval_seconds", default=45.0),
        45.0,
    )
    paired_exit_max_loss_fraction = _parse_nonnegative_float(
        "paired_exit_max_loss_fraction",
        _cfg_get(raw, defaults, "paired_exit_max_loss_fraction", default=0.35),
        0.35,
    )
    strategy_mode = str(
        _cfg_get(raw, defaults, "strategy_mode", default="paired_inventory_accumulation")
    ).strip().lower() or "paired_inventory_accumulation"
    late_entry_enabled = False
    entry_price_threshold = cheap_entry_price
    max_slippage = _parse_nonnegative_float(
        "max_slippage",
        os.getenv("MAX_SLIPPAGE", _cfg_get(raw, defaults, "max_slippage", "late_entry_max_slippage", default=0.005)),
        0.005,
    )
    late_entry_max_slippage = max_slippage
    max_hedge_retries = int(_cfg_get(raw, defaults, "max_hedge_retries", "MAX_HEDGE_RETRIES", default=3) or 3)
    env_retries = os.getenv("MAX_HEDGE_RETRIES", "").strip()
    if env_retries:
        max_hedge_retries = int(env_retries)
    if max_hedge_retries < 0:
        _fatal(f"{asset}:{window}: max_hedge_retries must be >= 0")
    hedge_retry_delay_ms = _parse_cooldown_ms(
        "hedge_retry_delay_ms",
        os.getenv("HEDGE_RETRY_DELAY_MS", _cfg_get(raw, defaults, "hedge_retry_delay_ms", default=400)),
        400,
    )
    max_market_exposure = _parse_nonnegative_float(
        "max_market_exposure",
        os.getenv("MAX_MARKET_EXPOSURE", _cfg_get(raw, defaults, "max_market_exposure", "inventory_max_capital_per_market", default=40.0)),
        40.0,
    )
    max_position_size = _parse_max_shares(
        "max_position_size",
        os.getenv("MAX_POSITION_SIZE", _cfg_get(raw, defaults, "max_position_size", "max_shares", default=20.0)),
        20.0,
    )
    max_unmatched_yes = _parse_max_shares(
        "max_unmatched_yes",
        _cfg_get(raw, defaults, "max_unmatched_yes", "gabagool_max_unpaired_shares", default=max_position_size),
        max_position_size,
    )
    max_unmatched_no = _parse_max_shares(
        "max_unmatched_no",
        _cfg_get(raw, defaults, "max_unmatched_no", "gabagool_max_unpaired_shares", default=max_position_size),
        max_position_size,
    )
    max_share_imbalance = _parse_nonnegative_float(
        "max_share_imbalance",
        os.getenv("MAX_SHARE_IMBALANCE", _cfg_get(raw, defaults, "max_share_imbalance", default=0.0)),
        0.0,
    )
    max_unmatched_shares = _parse_nonnegative_float(
        "max_unmatched_shares",
        os.getenv("MAX_UNMATCHED_SHARES", _cfg_get(raw, defaults, "max_unmatched_shares", default=0.0)),
        0.0,
    )
    balance_inventory_before_buy = _parse_bool_value(
        "balance_inventory_before_buy",
        os.getenv(
            "BALANCE_INVENTORY_BEFORE_BUY",
            _cfg_get(raw, defaults, "balance_inventory_before_buy", default=True),
        ),
        True,
    )
    account_for_pending_buy_orders = _parse_bool_value(
        "account_for_pending_buy_orders",
        os.getenv(
            "ACCOUNT_FOR_PENDING_BUY_ORDERS",
            _cfg_get(raw, defaults, "account_for_pending_buy_orders", default=True),
        ),
        True,
    )
    allow_unmatched_accumulation = _parse_bool_value(
        "allow_unmatched_accumulation",
        os.getenv(
            "ALLOW_UNMATCHED_ACCUMULATION",
            _cfg_get(raw, defaults, "allow_unmatched_accumulation", default=False),
        ),
        False,
    )
    if max_unmatched_shares > 0:
        max_unmatched_yes = min(max_unmatched_yes, max_unmatched_shares)
        max_unmatched_no = min(max_unmatched_no, max_unmatched_shares)
    min_order_size = _parse_order_size(
        "min_order_size",
        os.getenv("MIN_ORDER_SIZE", _cfg_get(raw, defaults, "min_order_size", default=MIN_SHARES)),
        float(MIN_SHARES),
    )
    if min_order_size < MIN_SHARES:
        _fatal(f"{asset}:{window}: min_order_size must be >= exchange minimum {MIN_SHARES}")
    max_quote_age_seconds = _parse_nonnegative_float(
        "max_quote_age_seconds",
        _cfg_get(
            raw, defaults, "max_quote_age_seconds",
            "spread_capture_max_data_age_seconds", default=3.0,
        ),
        3.0,
    )
    min_time_before_close_seconds = int(
        _cfg_get(
            raw, defaults, "min_time_before_close_seconds",
            "gabagool_min_time_to_resolution", default=0,
        )
    )
    if min_time_before_close_seconds < 0:
        _fatal(f"{asset}:{window}: min_time_before_close_seconds must be >= 0")

    stop_loss_exit_max_retries = int(
        _cfg_get(raw, defaults, "stop_loss_exit_max_retries", default=5) or 5
    )
    stop_loss_exit_retry_ms = _parse_cooldown_ms(
        "stop_loss_exit_retry_ms",
        _cfg_get(raw, defaults, "stop_loss_exit_retry_ms", default=400),
        400,
    )
    if stop_loss_exit_max_retries < 0:
        _fatal(f"{asset}:{window}: stop_loss_exit_max_retries must be >= 0")
    gabagool_initial_entry_threshold = _parse_unit_fraction(
        "gabagool_initial_entry_threshold",
        _cfg_get(raw, defaults, "gabagool_initial_entry_threshold"),
        float(defaults.get("gabagool_initial_entry_threshold", 0.90)),
    )
    gabagool_max_pair_cost = _parse_unit_fraction(
        "gabagool_max_pair_cost",
        _cfg_get(raw, defaults, "gabagool_max_pair_cost"),
        float(defaults.get("gabagool_max_pair_cost", 0.98)),
    )
    gabagool_min_profit_margin = _parse_unit_fraction(
        "gabagool_min_profit_margin",
        _cfg_get(raw, defaults, "gabagool_min_profit_margin"),
        float(defaults.get("gabagool_min_profit_margin", 0.02)),
    )
    gabagool_max_unpaired_shares = _parse_max_shares(
        "gabagool_max_unpaired_shares",
        _cfg_get(raw, defaults, "gabagool_max_unpaired_shares"),
        float(defaults.get("gabagool_max_unpaired_shares", 5.0)),
    )
    gabagool_min_time_to_resolution = int(
        _cfg_get(raw, defaults, "gabagool_min_time_to_resolution", default=60)
    )
    gabagool_price_buffer = float(
        _cfg_get(raw, defaults, "gabagool_price_buffer", default=0.0)
    )
    gabagool_inventory_soft_limit = _parse_nonnegative_float(
        "gabagool_inventory_soft_limit",
        _cfg_get(raw, defaults, "gabagool_inventory_soft_limit"),
        float(defaults.get("gabagool_inventory_soft_limit", 2.0)),
    )
    gabagool_endgame_seconds = int(
        _cfg_get(raw, defaults, "gabagool_endgame_seconds", default=30)
    )
    gabagool_endgame_max_unpaired = _parse_nonnegative_float(
        "gabagool_endgame_max_unpaired",
        _cfg_get(raw, defaults, "gabagool_endgame_max_unpaired"),
        float(defaults.get("gabagool_endgame_max_unpaired", 1.0)),
    )
    if gabagool_min_time_to_resolution < 0:
        _fatal(f"{asset}:{window}: gabagool_min_time_to_resolution must be >= 0")
    if gabagool_price_buffer < 0 or gabagool_price_buffer > 0.05:
        _fatal(f"{asset}:{window}: gabagool_price_buffer must be between 0 and 0.05")
    if gabagool_min_profit_margin >= 1:
        _fatal(f"{asset}:{window}: gabagool_min_profit_margin must be < 1")
    if gabagool_inventory_soft_limit > gabagool_max_unpaired_shares + 1e-9:
        _fatal(
            f"{asset}:{window}: gabagool_inventory_soft_limit={gabagool_inventory_soft_limit} "
            f"cannot exceed gabagool_max_unpaired_shares={gabagool_max_unpaired_shares}"
        )
    if gabagool_endgame_seconds < 0:
        _fatal(f"{asset}:{window}: gabagool_endgame_seconds must be >= 0")
    if gabagool_endgame_max_unpaired > gabagool_max_unpaired_shares + 1e-9:
        _fatal(
            f"{asset}:{window}: gabagool_endgame_max_unpaired={gabagool_endgame_max_unpaired} "
            f"cannot exceed gabagool_max_unpaired_shares={gabagool_max_unpaired_shares}"
        )
    gabagool_falling_knife_enabled = _parse_bool_value(
        "gabagool_falling_knife_enabled",
        _cfg_get(raw, defaults, "gabagool_falling_knife_enabled", default=True),
        True,
    )
    gabagool_falling_knife_window_seconds = _parse_nonnegative_float(
        "gabagool_falling_knife_window_seconds",
        _cfg_get(raw, defaults, "gabagool_falling_knife_window_seconds", default=3.0),
        3.0,
    )
    gabagool_falling_knife_min_drop = _parse_nonnegative_float(
        "gabagool_falling_knife_min_drop",
        _cfg_get(raw, defaults, "gabagool_falling_knife_min_drop", default=0.035),
        0.035,
    )
    gabagool_falling_knife_fast_window_seconds = _parse_nonnegative_float(
        "gabagool_falling_knife_fast_window_seconds",
        _cfg_get(raw, defaults, "gabagool_falling_knife_fast_window_seconds", default=1.0),
        1.0,
    )
    gabagool_falling_knife_fast_drop = _parse_nonnegative_float(
        "gabagool_falling_knife_fast_drop",
        _cfg_get(raw, defaults, "gabagool_falling_knife_fast_drop", default=0.015),
        0.015,
    )
    gabagool_falling_knife_recovery_cents = _parse_nonnegative_float(
        "gabagool_falling_knife_recovery_cents",
        _cfg_get(raw, defaults, "gabagool_falling_knife_recovery_cents", default=1.5),
        1.5,
    )
    gabagool_falling_knife_cooldown_seconds = _parse_nonnegative_float(
        "gabagool_falling_knife_cooldown_seconds",
        _cfg_get(raw, defaults, "gabagool_falling_knife_cooldown_seconds", default=5.0),
        5.0,
    )
    if gabagool_falling_knife_window_seconds <= 0:
        _fatal(f"{asset}:{window}: gabagool_falling_knife_window_seconds must be > 0")
    if gabagool_falling_knife_fast_window_seconds <= 0:
        _fatal(f"{asset}:{window}: gabagool_falling_knife_fast_window_seconds must be > 0")
    if gabagool_falling_knife_fast_window_seconds > gabagool_falling_knife_window_seconds:
        _fatal(f"{asset}:{window}: falling-knife fast window cannot exceed main window")
    if gabagool_falling_knife_min_drop <= 0 and gabagool_falling_knife_fast_drop <= 0:
        _fatal(f"{asset}:{window}: at least one falling-knife drop threshold must be > 0")
    if gabagool_max_pair_cost > 1.0 - gabagool_min_profit_margin + 1e-9:
        _fatal(
            f"{asset}:{window}: gabagool_max_pair_cost={gabagool_max_pair_cost} "
            f"must be <= 1 - gabagool_min_profit_margin={1.0 - gabagool_min_profit_margin:.4f}"
        )

    momentum_engine_enabled = _parse_bool_value("momentum_engine_enabled", _cfg_get(raw, defaults, "momentum_engine_enabled", default=True), True)
    momentum_min_delta = _parse_nonnegative_float("momentum_min_delta", _cfg_get(raw, defaults, "momentum_min_delta", default=0.0015), 0.0015)
    momentum_signal_max_age = _parse_nonnegative_float("momentum_signal_max_age", _cfg_get(raw, defaults, "momentum_signal_max_age", default=0.75), 0.75)
    momentum_execution_mode = str(_cfg_get(raw, defaults, "momentum_execution_mode", default="single_taker")).strip().lower()
    if momentum_execution_mode not in {"single_taker", "gtc_at_ask", "single_maker", "dual_hybrid"}:
        _fatal(f"{asset}:{window}: unsupported momentum_execution_mode={momentum_execution_mode!r}")
    momentum_max_position_delta = _parse_nonnegative_float("momentum_max_position_delta", _cfg_get(raw, defaults, "momentum_max_position_delta", default=5.0), 5.0)
    market_making_enabled = _parse_bool_value("market_making_enabled", _cfg_get(raw, defaults, "market_making_enabled", default=True), True)
    market_making_quote_size = _parse_order_size("market_making_quote_size", _cfg_get(raw, defaults, "market_making_quote_size", default=5.0), 5.0)
    market_making_min_spread = _parse_nonnegative_float("market_making_min_spread", _cfg_get(raw, defaults, "market_making_min_spread", default=0.01), 0.01)
    market_making_quote_offset = _parse_nonnegative_float("market_making_quote_offset", _cfg_get(raw, defaults, "market_making_quote_offset", default=0.01), 0.01)
    market_making_max_position_delta = _parse_nonnegative_float("market_making_max_position_delta", _cfg_get(raw, defaults, "market_making_max_position_delta", default=5.0), 5.0)
    market_making_requote_seconds = _parse_nonnegative_float("market_making_requote_seconds", _cfg_get(raw, defaults, "market_making_requote_seconds", default=0.40), 0.40)
    spread_capture_enabled = _parse_bool_value("spread_capture_enabled", _cfg_get(raw, defaults, "spread_capture_enabled", default=True), True)
    spread_capture_trigger_sum = _parse_nonnegative_float("spread_capture_trigger_sum", _cfg_get(raw, defaults, "spread_capture_trigger_sum", default=0.995), 0.995)
    spread_capture_max_entry_sum = _parse_nonnegative_float("spread_capture_max_entry_sum", _cfg_get(raw, defaults, "spread_capture_max_entry_sum", default=0.985), 0.985)
    spread_capture_max_unpaired = _parse_nonnegative_float("spread_capture_max_unpaired", _cfg_get(raw, defaults, "spread_capture_max_unpaired", default=1.0), 1.0)
    spread_capture_min_net_profit = _parse_nonnegative_float(
        "spread_capture_min_net_profit",
        _cfg_get(raw, defaults, "spread_capture_min_net_profit", default=0.005),
        0.005,
    )
    spread_capture_fee_rate = _parse_nonnegative_float(
        "spread_capture_fee_rate",
        _cfg_get(raw, defaults, "spread_capture_fee_rate", default=0.0),
        0.0,
    )
    spread_capture_slippage_buffer = _parse_nonnegative_float(
        "spread_capture_slippage_buffer",
        _cfg_get(raw, defaults, "spread_capture_slippage_buffer", default=0.002),
        0.002,
    )
    spread_capture_min_book_liquidity = _parse_order_size(
        "spread_capture_min_book_liquidity",
        _cfg_get(raw, defaults, "spread_capture_min_book_liquidity", default=5.0),
        5.0,
    )
    spread_capture_max_data_age_seconds = _parse_nonnegative_float(
        "spread_capture_max_data_age_seconds",
        _cfg_get(raw, defaults, "spread_capture_max_data_age_seconds", default=3.0),
        3.0,
    )
    spread_capture_second_leg_timeout_ms = _parse_cooldown_ms(
        "spread_capture_second_leg_timeout_ms",
        _cfg_get(raw, defaults, "spread_capture_second_leg_timeout_ms", default=10000),
        10000,
    )
    spread_capture_max_recovery_loss = _parse_nonnegative_float(
        "spread_capture_max_recovery_loss",
        _cfg_get(raw, defaults, "spread_capture_max_recovery_loss", default=2.0),
        2.0,
    )
    spread_capture_release_blocked_to_eligible = _parse_bool_value(
        "spread_capture_release_blocked_to_eligible",
        _cfg_get(raw, defaults, "spread_capture_release_blocked_to_eligible", default=False),
        False,
    )
    inventory_accumulation_enabled = _parse_bool_value(
        "inventory_accumulation_enabled",
        _cfg_get(raw, defaults, "inventory_accumulation_enabled", default=True),
        True,
    )
    inventory_max_pair_cost = _parse_unit_fraction(
        "inventory_max_pair_cost",
        _cfg_get(raw, defaults, "inventory_max_pair_cost", default=gabagool_max_pair_cost),
        gabagool_max_pair_cost,
    )
    inventory_min_net_edge = _parse_nonnegative_float(
        "inventory_min_net_edge",
        _cfg_get(raw, defaults, "inventory_min_net_edge", default=gabagool_min_profit_margin),
        gabagool_min_profit_margin,
    )
    inventory_max_capital_per_market = _parse_nonnegative_float(
        "inventory_max_capital_per_market",
        _cfg_get(raw, defaults, "inventory_max_capital_per_market", default=40.0),
        40.0,
    )
    inventory_max_dca_attempts = int(
        _cfg_get(raw, defaults, "inventory_max_dca_attempts", default=8) or 8
    )
    inventory_dca_cooldown_seconds = _parse_nonnegative_float(
        "inventory_dca_cooldown_seconds",
        _cfg_get(raw, defaults, "inventory_dca_cooldown_seconds", default=60.0),
        60.0,
    )
    inventory_dca_min_price_move = _parse_nonnegative_float(
        "inventory_dca_min_price_move",
        _cfg_get(raw, defaults, "inventory_dca_min_price_move", default=0.03),
        0.03,
    )
    inventory_max_imbalance_seconds = _parse_nonnegative_float(
        "inventory_max_imbalance_seconds",
        _cfg_get(raw, defaults, "inventory_max_imbalance_seconds", default=7200.0),
        7200.0,
    )
    inventory_max_active_orders = int(
        _cfg_get(raw, defaults, "inventory_max_active_orders", default=4) or 4
    )
    min_net_profit_per_pair = _parse_nonnegative_float(
        "min_net_profit_per_pair",
        os.getenv(
            "MIN_NET_PROFIT_PER_PAIR",
            _cfg_get(
                raw,
                defaults,
                "min_net_profit_per_pair",
                "inventory_min_net_edge",
                "gabagool_min_profit_margin",
                default=0.02,
            ),
        ),
        0.02,
    )
    force_hedge_after_timeout = _parse_bool_value(
        "force_hedge_after_timeout",
        os.getenv(
            "FORCE_HEDGE_AFTER_TIMEOUT",
            _cfg_get(raw, defaults, "force_hedge_after_timeout", default=False),
        ),
        False,
    )
    max_consecutive_buys_raw = _cfg_get(
        raw, defaults, "max_consecutive_buys_per_outcome", default=3,
    )
    try:
        max_consecutive_buys_per_outcome = int(max_consecutive_buys_raw)
    except (TypeError, ValueError):
        _fatal(f"max_consecutive_buys_per_outcome={max_consecutive_buys_raw!r} is not a valid integer")
    if max_consecutive_buys_per_outcome < 0:
        _fatal(f"{asset}:{window}: max_consecutive_buys_per_outcome must be >= 0")
    stabilization_window_seconds = _parse_nonnegative_float(
        "stabilization_window_seconds",
        _cfg_get(raw, defaults, "stabilization_window_seconds", default=10.0),
        10.0,
    )
    stabilization_tolerance_percent = _parse_nonnegative_float(
        "stabilization_tolerance_percent",
        _cfg_get(raw, defaults, "stabilization_tolerance_percent", default=0.20),
        0.20,
    )
    short_window_seconds = _parse_nonnegative_float(
        "short_window_seconds",
        _cfg_get(raw, defaults, "short_window_seconds", default=5.0),
        5.0,
    )
    max_short_window_move_percent = _parse_nonnegative_float(
        "max_short_window_move_percent",
        _cfg_get(raw, defaults, "max_short_window_move_percent", default=0.15),
        0.15,
    )
    if short_window_seconds <= 0:
        _fatal(f"{asset}:{window}: short_window_seconds must be > 0")
    if short_window_seconds > stabilization_window_seconds:
        _fatal(f"{asset}:{window}: short_window_seconds cannot exceed stabilization_window_seconds")
    if stabilization_tolerance_percent <= 0 or max_short_window_move_percent <= 0:
        _fatal(f"{asset}:{window}: stability percentage thresholds must be > 0")

    latency_arb_enabled = _parse_bool_value(
        "latency_arb_enabled",
        _cfg_get(
            raw,
            defaults,
            "latency_arb_enabled",
            "momentum_engine_enabled",
            default=True,
        ),
        True,
    )
    lookback_secs = _parse_nonnegative_float(
        "lookback_secs",
        _cfg_get(
            raw,
            defaults,
            "lookback_secs",
            default=2.0,
        ),
        2.0,
    )
    entry_min_delta = _parse_nonnegative_float(
        "entry_min_delta",
        _cfg_get(
            raw,
            defaults,
            "entry_min_delta",
            default=0.01,
        ),
        0.01,
    )
    eval_interval_ms = _parse_cooldown_ms(
        "eval_interval_ms",
        _cfg_get(
            raw,
            defaults,
            "eval_interval_ms",
            "trade_cooldown_ms",
            default=1000,
        ),
        1000,
    )
    entry_delay_secs = _parse_nonnegative_float(
        "entry_delay_secs",
        _cfg_get(raw, defaults, "entry_delay_secs", default=3.0),
        3.0,
    )
    latency_arb_min_ask = _parse_unit_fraction(
        "latency_arb_min_ask",
        _cfg_get(
            raw,
            defaults,
            "min_price",
            "latency_arb_min_ask",
            "entry_price",
            default=entry_price,
        ),
        entry_price,
    )
    latency_arb_max_ask = _parse_unit_fraction(
        "latency_arb_max_ask",
        _cfg_get(
            raw,
            defaults,
            "max_price",
            "latency_arb_max_ask",
            default=0.99,
        ),
        0.99,
    )
    latency_arb_signal_max_age_seconds = _parse_nonnegative_float(
        "latency_arb_signal_max_age_seconds",
        _cfg_get(
            raw,
            defaults,
            "latency_arb_signal_max_age_seconds",
            "momentum_signal_max_age",
            default=0.75,
        ),
        0.75,
    )
    if lookback_secs <= 0:
        _fatal(f"{asset}:{window}: lookback_secs must be > 0")
    if entry_min_delta <= 0:
        _fatal(f"{asset}:{window}: entry_min_delta must be > 0")
    if latency_arb_signal_max_age_seconds <= 0:
        _fatal(f"{asset}:{window}: latency_arb_signal_max_age_seconds must be > 0")
    if latency_arb_min_ask >= latency_arb_max_ask:
        _fatal(f"{asset}:{window}: min_price/latency_arb_min_ask must be < max_price")

    endgame_enabled = _parse_bool_value(
        "endgame_enabled",
        _cfg_get(raw, defaults, "endgame_enabled", default=True),
        True,
    )
    hold_to_settle = _parse_bool_value(
        "hold_to_settle",
        _cfg_get(raw, defaults, "hold_to_settle", default=True),
        True,
    )
    endgame_max_seconds_left = _parse_nonnegative_float(
        "endgame_max_seconds_left",
        _cfg_get(raw, defaults, "endgame_max_seconds_left", default=60.0),
        60.0,
    )
    endgame_min_seconds_left = _parse_nonnegative_float(
        "endgame_min_seconds_left",
        _cfg_get(
            raw,
            defaults,
            "endgame_min_seconds_left",
            "min_entry_seconds_left",
            default=15.0,
        ),
        15.0,
    )
    endgame_min_move_bps = _parse_nonnegative_float(
        "endgame_min_move_bps",
        _cfg_get(raw, defaults, "endgame_min_move_bps", default=10.0),
        10.0,
    )
    if endgame_enabled:
        if endgame_max_seconds_left <= 0:
            _fatal(f"{asset}:{window}: endgame_max_seconds_left must be > 0")
        if endgame_min_seconds_left < 0:
            _fatal(f"{asset}:{window}: endgame_min_seconds_left must be >= 0")
        if endgame_min_seconds_left >= endgame_max_seconds_left:
            _fatal(
                f"{asset}:{window}: endgame_min_seconds_left must be < endgame_max_seconds_left"
            )
        if endgame_min_move_bps <= 0:
            _fatal(f"{asset}:{window}: endgame_min_move_bps must be > 0")

    min_orderbook_depth = _parse_order_size(
        "min_orderbook_depth",
        os.getenv(
            "MIN_ORDERBOOK_DEPTH",
            _cfg_get(
                raw,
                defaults,
                "min_orderbook_depth",
                "spread_capture_min_book_liquidity",
                default=5.0,
            ),
        ),
        5.0,
    )
    falling_knife_filter_enabled = _parse_bool_value(
        "falling_knife_filter_enabled",
        os.getenv(
            "FALLING_KNIFE_FILTER_ENABLED",
            _cfg_get(
                raw,
                defaults,
                "falling_knife_filter_enabled",
                "gabagool_falling_knife_enabled",
                default=True,
            ),
        ),
        True,
    )
    price_drop_window_seconds = _parse_nonnegative_float(
        "price_drop_window_seconds",
        os.getenv(
            "PRICE_DROP_WINDOW_SECONDS",
            _cfg_get(
                raw,
                defaults,
                "price_drop_window_seconds",
                "gabagool_falling_knife_window_seconds",
                default=15.0,
            ),
        ),
        15.0,
    )
    max_price_drop_percent = _parse_nonnegative_float(
        "max_price_drop_percent",
        os.getenv(
            "MAX_PRICE_DROP_PERCENT",
            _cfg_get(raw, defaults, "max_price_drop_percent", default=3.5),
        ),
        3.5,
    )
    falling_knife_cooldown_seconds = _parse_nonnegative_float(
        "falling_knife_cooldown_seconds",
        os.getenv(
            "FALLING_KNIFE_COOLDOWN_SECONDS",
            _cfg_get(
                raw,
                defaults,
                "falling_knife_cooldown_seconds",
                "gabagool_falling_knife_cooldown_seconds",
                default=5.0,
            ),
        ),
        5.0,
    )
    if price_drop_window_seconds <= 0:
        _fatal(f"{asset}:{window}: price_drop_window_seconds must be > 0")
    if stabilization_window_seconds <= 0:
        _fatal(f"{asset}:{window}: stabilization_window_seconds must be > 0")
    # Prefer explicit trade_cooldown_ms; otherwise mirror arbigab eval_interval_ms.
    trade_cooldown_ms = _parse_cooldown_ms(
        "trade_cooldown_ms",
        _cfg_get(raw, defaults, "trade_cooldown_ms", "eval_interval_ms", default=eval_interval_ms),
        int(eval_interval_ms),
    )

    order_size_fixed = _parse_order_size(
        "order_size",
        _cfg_get(raw, defaults, "order_size", "spread_size"),
        float(_cfg_get(defaults, {}, "order_size", "spread_size", default=5.0)),
    )
    size_min_raw = _cfg_get(raw, defaults, "order_size_min", "spread_size_min")
    size_max_raw = _cfg_get(raw, defaults, "order_size_max", "spread_size_max")
    if size_min_raw is None and size_max_raw is None:
        order_size_min = order_size_max = order_size_fixed
    else:
        order_size_min = _parse_order_size(
            "order_size_min",
            size_min_raw if size_min_raw is not None else order_size_fixed,
            order_size_fixed,
        )
        order_size_max = _parse_order_size(
            "order_size_max",
            size_max_raw if size_max_raw is not None else order_size_fixed,
            order_size_fixed,
        )
    max_order = _parse_order_size(
        "max_order_size",
        _cfg_get(raw, defaults, "max_order_size"),
        float(defaults.get("max_order_size", 5.0)),
    )
    max_shares = _parse_max_shares(
        "max_shares",
        _cfg_get(raw, defaults, "max_shares"),
        float(defaults.get("max_shares", 20.0)),
    )

    if ENV_SIZING_OVERRIDES:
        order_size_min = ENV_SIZING_OVERRIDES["order_size_min"]
        order_size_max = ENV_SIZING_OVERRIDES["order_size_max"]
        max_shares = ENV_SIZING_OVERRIDES["max_shares"]
        if "max_order_size" in ENV_SIZING_OVERRIDES:
            max_order = ENV_SIZING_OVERRIDES["max_order_size"]

    if order_size_min > order_size_max:
        _fatal(
            f"{asset}:{window}: order_size_min ({order_size_min}) "
            f"cannot exceed order_size_max ({order_size_max})"
        )
    max_order = max(max_order, order_size_max)
    if max_order > max_shares:
        _fatal(
            f"{asset}:{window}: max_order_size ({max_order}) "
            f"cannot exceed max_shares ({max_shares})"
        )

    dr_raw = _cfg_get(raw, defaults, "dry_run")
    if dr_raw is None:
        dry_run = DRY_RUN_DEFAULT
    else:
        dry_run = bool(dr_raw)

    dry_min = _parse_cooldown_ms(
        "dry_run_fill_delay_min_ms",
        _cfg_get(raw, defaults, "dry_run_fill_delay_min_ms"),
        int(defaults.get("dry_run_fill_delay_min_ms", 200)),
    )
    dry_max = _parse_cooldown_ms(
        "dry_run_fill_delay_max_ms",
        _cfg_get(raw, defaults, "dry_run_fill_delay_max_ms"),
        int(defaults.get("dry_run_fill_delay_max_ms", 2500)),
    )
    if dry_max < dry_min:
        _fatal(f"{asset}:{window}: dry_run_fill_delay_max_ms must be >= dry_run_fill_delay_min_ms")

    interval = WINDOW_SECONDS[window]
    listener_raw = _cfg_get(raw, defaults, "listener_activate_secs")
    entry_raw = _cfg_get(raw, defaults, "entry_seconds_left")
    env_listener = os.getenv("LISTENER_ACTIVATE_SECONDS", "").strip()
    env_entry = os.getenv("ENTRY_SECONDS_LEFT", "").strip()
    if listener_raw is not None:
        listener_secs = int(listener_raw)
    elif env_listener:
        listener_secs = int(env_listener)
    else:
        listener_secs = interval
    if entry_raw is not None:
        entry_secs = int(entry_raw)
    elif env_entry:
        entry_secs = int(env_entry)
    else:
        entry_secs = interval

    min_entry_raw = _cfg_get(raw, defaults, "min_entry_seconds_left")
    fill_timeout_raw = _cfg_get(
        raw, defaults, "fill_timeout_ms", "spread_fill_timeout_ms",
    )
    fill_poll_raw = _cfg_get(raw, defaults, "fill_poll_ms", "spread_fill_poll_ms")

    min_entry_secs = int(min_entry_raw if min_entry_raw is not None else 0)
    fill_timeout_ms = _parse_cooldown_ms(
        "fill_timeout_ms",
        fill_timeout_raw,
        int(_cfg_get(defaults, {}, "fill_timeout_ms", "spread_fill_timeout_ms", default=10000)),
    )
    fill_poll_ms = _parse_cooldown_ms(
        "fill_poll_ms",
        fill_poll_raw,
        int(_cfg_get(defaults, {}, "fill_poll_ms", "spread_fill_poll_ms", default=400)),
    )

    if min_entry_secs < 0:
        _fatal(f"{asset}:{window}: min_entry_seconds_left must be >= 0")
    if fill_poll_ms > fill_timeout_ms:
        _fatal(f"{asset}:{window}: fill_poll_ms must be <= fill_timeout_ms")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).lower() in ("1", "true", "yes", "on")

    return WorkerConfig(
        asset=asset,
        window=window,
        momentum_entry_threshold=momentum_entry_threshold,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        entry_price=entry_price,
        price_99_entry_window_seconds=price_99_entry_window_seconds,
        inventory_accumulation_enabled=inventory_accumulation_enabled,
        cheap_side_min_price=cheap_side_min_price,
        cheap_side_max_price=cheap_side_max_price,
        target_combined_pair_cost=target_combined_pair_cost,
        max_combined_pair_cost=max_combined_pair_cost,
        paired_initial_entry_threshold=paired_initial_entry_threshold,
        paired_initial_entry_direction=paired_initial_entry_direction,
        paired_max_inventory_per_side=paired_max_inventory_per_side,
        paired_unmatched_inventory_target=paired_unmatched_inventory_target,
        paired_dca_increment=paired_dca_increment,
        paired_max_dca_legs_per_side=paired_max_dca_legs_per_side,
        paired_allow_adaptive_dca=paired_allow_adaptive_dca,
        paired_enable_dynamic_hedge_pricing=paired_enable_dynamic_hedge_pricing,
        paired_enable_adaptive_hedge_timeout=paired_enable_adaptive_hedge_timeout,
        paired_enable_adaptive_risk=paired_enable_adaptive_risk,
        paired_live_trading_enabled=paired_live_trading_enabled,
        paired_early_min_seconds_left=paired_early_min_seconds_left,
        paired_middle_min_seconds_left=paired_middle_min_seconds_left,
        paired_stop_new_accumulation_seconds=paired_stop_new_accumulation_seconds,
        paired_force_exit_eval_seconds=paired_force_exit_eval_seconds,
        paired_exit_max_loss_fraction=paired_exit_max_loss_fraction,
        strategy_mode=strategy_mode,
        max_entries_per_side_per_market=max_entries_per_side_per_market,
        no_buys_last_minutes=no_buys_last_minutes,
        cheap_entry_price=cheap_entry_price,
        initial_hedge_delay_seconds=initial_hedge_delay_seconds,
        combined_cost_target=combined_cost_target,
        max_combined_cost=max_combined_cost,
        entry_window_seconds=entry_window_seconds,
        max_entry_price=max_entry_price,
        order_type=order_type,
        max_slippage=max_slippage,
        max_hedge_retries=max_hedge_retries,
        hedge_retry_delay_ms=hedge_retry_delay_ms,
        max_market_exposure=max_market_exposure,
        max_position_size=max_position_size,
        max_unmatched_yes=max_unmatched_yes,
        max_unmatched_no=max_unmatched_no,
        max_share_imbalance=max_share_imbalance,
        max_unmatched_shares=max_unmatched_shares,
        balance_inventory_before_buy=balance_inventory_before_buy,
        account_for_pending_buy_orders=account_for_pending_buy_orders,
        allow_unmatched_accumulation=allow_unmatched_accumulation,
        min_order_size=min_order_size,
        late_entry_enabled=late_entry_enabled,
        entry_price_threshold=entry_price,
        late_entry_max_slippage=late_entry_max_slippage,
        max_quote_age_seconds=max_quote_age_seconds,
        min_time_before_close_seconds=min_time_before_close_seconds,
        stop_loss_exit_max_retries=stop_loss_exit_max_retries,
        stop_loss_exit_retry_ms=stop_loss_exit_retry_ms,
        gabagool_initial_entry_threshold=gabagool_initial_entry_threshold,
        gabagool_max_pair_cost=gabagool_max_pair_cost,
        gabagool_min_profit_margin=gabagool_min_profit_margin,
        gabagool_max_unpaired_shares=gabagool_max_unpaired_shares,
        gabagool_min_time_to_resolution=gabagool_min_time_to_resolution,
        gabagool_price_buffer=gabagool_price_buffer,
        gabagool_inventory_soft_limit=gabagool_inventory_soft_limit,
        gabagool_endgame_seconds=gabagool_endgame_seconds,
        gabagool_endgame_max_unpaired=gabagool_endgame_max_unpaired,
        gabagool_falling_knife_enabled=gabagool_falling_knife_enabled,
        gabagool_falling_knife_window_seconds=gabagool_falling_knife_window_seconds,
        gabagool_falling_knife_min_drop=gabagool_falling_knife_min_drop,
        gabagool_falling_knife_fast_window_seconds=gabagool_falling_knife_fast_window_seconds,
        gabagool_falling_knife_fast_drop=gabagool_falling_knife_fast_drop,
        gabagool_falling_knife_recovery_cents=gabagool_falling_knife_recovery_cents,
        gabagool_falling_knife_cooldown_seconds=gabagool_falling_knife_cooldown_seconds,
        momentum_engine_enabled=momentum_engine_enabled,
        momentum_min_delta=momentum_min_delta,
        momentum_signal_max_age=momentum_signal_max_age,
        momentum_execution_mode=momentum_execution_mode,
        momentum_max_position_delta=momentum_max_position_delta,
        market_making_enabled=market_making_enabled,
        market_making_quote_size=market_making_quote_size,
        market_making_min_spread=market_making_min_spread,
        market_making_quote_offset=market_making_quote_offset,
        market_making_max_position_delta=market_making_max_position_delta,
        market_making_requote_seconds=market_making_requote_seconds,
        spread_capture_enabled=spread_capture_enabled,
        spread_capture_trigger_sum=spread_capture_trigger_sum,
        spread_capture_max_entry_sum=spread_capture_max_entry_sum,
        spread_capture_max_unpaired=spread_capture_max_unpaired,
        spread_capture_min_net_profit=spread_capture_min_net_profit,
        spread_capture_fee_rate=spread_capture_fee_rate,
        spread_capture_slippage_buffer=spread_capture_slippage_buffer,
        spread_capture_min_book_liquidity=spread_capture_min_book_liquidity,
        spread_capture_max_data_age_seconds=spread_capture_max_data_age_seconds,
        spread_capture_second_leg_timeout_ms=spread_capture_second_leg_timeout_ms,
        spread_capture_max_recovery_loss=spread_capture_max_recovery_loss,
        spread_capture_release_blocked_to_eligible=spread_capture_release_blocked_to_eligible,
        inventory_max_pair_cost=inventory_max_pair_cost,
        inventory_min_net_edge=inventory_min_net_edge,
        inventory_max_capital_per_market=inventory_max_capital_per_market,
        inventory_max_dca_attempts=inventory_max_dca_attempts,
        inventory_dca_cooldown_seconds=inventory_dca_cooldown_seconds,
        inventory_dca_min_price_move=inventory_dca_min_price_move,
        inventory_max_imbalance_seconds=inventory_max_imbalance_seconds,
        inventory_max_active_orders=inventory_max_active_orders,
        min_net_profit_per_pair=min_net_profit_per_pair,
        force_hedge_after_timeout=force_hedge_after_timeout,
        max_consecutive_buys_per_outcome=max_consecutive_buys_per_outcome,
        stabilization_window_seconds=stabilization_window_seconds,
        stabilization_tolerance_percent=stabilization_tolerance_percent,
        short_window_seconds=short_window_seconds,
        max_short_window_move_percent=max_short_window_move_percent,
        latency_arb_enabled=latency_arb_enabled,
        lookback_secs=lookback_secs,
        entry_min_delta=entry_min_delta,
        eval_interval_ms=eval_interval_ms,
        entry_delay_secs=entry_delay_secs,
        latency_arb_min_ask=latency_arb_min_ask,
        latency_arb_max_ask=latency_arb_max_ask,
        latency_arb_signal_max_age_seconds=latency_arb_signal_max_age_seconds,
        endgame_enabled=endgame_enabled,
        hold_to_settle=hold_to_settle,
        endgame_max_seconds_left=endgame_max_seconds_left,
        endgame_min_seconds_left=endgame_min_seconds_left,
        endgame_min_move_bps=endgame_min_move_bps,
        min_orderbook_depth=min_orderbook_depth,
        falling_knife_filter_enabled=falling_knife_filter_enabled,
        price_drop_window_seconds=price_drop_window_seconds,
        max_price_drop_percent=max_price_drop_percent,
        falling_knife_cooldown_seconds=falling_knife_cooldown_seconds,
        trade_cooldown_ms=trade_cooldown_ms,
        order_size_min=order_size_min,
        order_size_max=order_size_max,
        max_order_size=max_order,
        max_shares=max_shares,
        dry_run=dry_run,
        dry_run_fill_delay_min_ms=dry_min,
        dry_run_fill_delay_max_ms=dry_max,
        listener_activate_secs=listener_secs,
        entry_seconds_left=entry_secs,
        min_entry_seconds_left=min_entry_secs,
        fill_timeout_ms=fill_timeout_ms,
        fill_poll_ms=fill_poll_ms,
        enabled=enabled,
    )


def load_worker_configs(path: Optional[str] = None) -> Tuple[WorkerConfig, ...]:
    cfg_path = path or os.getenv("TRADING_CONFIG_PATH", "trading_config.json")
    if not os.path.isfile(cfg_path):
        _fatal(f"Trading config not found: {cfg_path}")

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        _fatal(f"Invalid JSON in {cfg_path}: {e}")
    except OSError as e:
        _fatal(f"Cannot read {cfg_path}: {e}")

    if not isinstance(data, dict):
        _fatal(f"{cfg_path} must be a JSON object.")

    defaults = data.get("defaults") or {}
    workers_raw = data.get("workers")
    if not isinstance(workers_raw, list) or not workers_raw:
        _fatal(f"{cfg_path} must contain a non-empty 'workers' array.")

    seen: set[str] = set()
    out: list[WorkerConfig] = []
    for entry in workers_raw:
        if not isinstance(entry, dict):
            _fatal("Each worker entry must be a JSON object.")
        wc = _merge_worker_entry(entry, defaults)
        if not wc.enabled:
            continue
        if wc.key in seen:
            _fatal(f"Duplicate worker config: {wc.key}")
        seen.add(wc.key)
        out.append(wc)

    if not out:
        _fatal("No enabled workers in trading config.")

    return tuple(out)


WORKER_CONFIGS: Tuple[WorkerConfig, ...] = load_worker_configs()
TRADING_ASSETS: Tuple[str, ...] = tuple(dict.fromkeys(w.asset for w in WORKER_CONFIGS))
TRADING_ASSETS_UPPER: Tuple[str, ...] = tuple(a.upper() for a in TRADING_ASSETS)
ALL_TRACKED_ASSETS = TRADING_ASSETS
TOTAL_BOTS: int = len(WORKER_CONFIGS)


def asset_pnl_filename(asset: str, window: str = "5m") -> str:
    a = normalize_asset_slug(asset)
    w = normalize_window(window)
    return f"{a}_{w}_pnl_history.json"


PNL_FILES: list[str] = [asset_pnl_filename(w.asset, w.window) for w in WORKER_CONFIGS]


def validate_trading_assets() -> Tuple[str, ...]:
    if not TRADING_ASSETS:
        _fatal("No trading assets resolved from worker config.")
    return TRADING_ASSETS


def trading_assets_label(separator: str = " · ") -> str:
    labels = [f"{w.asset.upper()} {w.window}" for w in WORKER_CONFIGS]
    return separator.join(labels)


def _parse_positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _fatal(f"{name}={raw!r} is not a valid number.")
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        _fatal(f"{name} must be a positive number (got {raw!r}).")
    return value


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _fatal(f"{name}={raw!r} is not a valid integer.")
    if value <= 0:
        _fatal(f"{name} must be a positive integer (got {raw!r}).")
    return value


print(
    f"📌 Workers ({len(WORKER_CONFIGS)}): "
    + ", ".join(f"{w.asset.upper()} {w.window}" for w in WORKER_CONFIGS)
)
print(f"🧪 DRY_RUN_DEFAULT={DRY_RUN_DEFAULT}")
if WORKER_CONFIGS:
    wc0 = WORKER_CONFIGS[0]
    if ENV_SIZING_OVERRIDES:
        size_label = (
            f"{wc0.order_size_min}-{wc0.order_size_max} random"
            if wc0.random_order_size
            else str(wc0.order_size_max)
        )
        print(
            f"📐 Sizing (.env override): order={size_label} | "
            f"max_order={wc0.max_order_size} | max_shares={wc0.max_shares}"
        )
    else:
        print(
            f"📐 Sizing (trading_config.json): order={wc0.order_size_max} fixed | "
            f"max_order={wc0.max_order_size} | max_shares={wc0.max_shares}"
        )
    if wc0.strategy_mode == "paired_inventory_accumulation":
        print(
            f"📈 Paired inventory: {wc0.paired_initial_entry_direction} "
            f"entry≥{wc0.paired_initial_entry_threshold:.2f} | "
            f"target_pair≤{wc0.target_combined_pair_cost:.2f} | "
            f"max/side={wc0.paired_max_inventory_per_side:.0f} | "
            f"unmatched_target={wc0.paired_unmatched_inventory_target:.0f} | "
            f"adaptive_dca={wc0.paired_allow_adaptive_dca} | "
            f"live={wc0.paired_live_trading_enabled} | "
            f"dry_run={wc0.dry_run} | order={wc0.order_size_max} sh"
        )
    else:
        print(
            f"📈 Entry: ask≥{wc0.entry_price:.2f} | "
            f"window=final {wc0.entry_window_seconds}s "
            f"(min_left={wc0.min_entry_seconds_left}s) | "
            f"band {wc0.latency_arb_min_ask:.2f}-{wc0.latency_arb_max_ask:.2f} | "
            f"stop={wc0.stop_loss_pct:.0%} | TP={wc0.take_profit_pct:.0%} | "
            f"hold_to_settle={wc0.hold_to_settle} | order={wc0.order_size_max} sh"
        )
