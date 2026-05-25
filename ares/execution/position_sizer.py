"""
Trygg Ares — Position Sizer
Calculates share quantity from GBP limit and live price.
Handles USD/GBP conversion for NASDAQ-listed stocks.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum position size in GBP
MAX_POSITION_GBP = float(os.getenv("IBKR_MAX_POSITION_GBP", "1000"))


def calculate_quantity(
    price_usd: float,
    gbp_usd_rate: float = 1.27,  # Fallback rate — updated at runtime
    max_position_gbp: float = MAX_POSITION_GBP,
    min_quantity: int = 1,
) -> tuple[int, float, float]:
    """
    Calculate share quantity from GBP limit and USD price.

    Returns:
        (quantity, position_value_usd, position_value_gbp)
    """
    if price_usd <= 0:
        return 0, 0.0, 0.0

    # Convert GBP limit to USD
    max_position_usd = max_position_gbp * gbp_usd_rate

    # Calculate raw quantity
    raw_quantity = max_position_usd / price_usd

    # Round down to whole shares
    quantity = max(min_quantity, int(raw_quantity))

    # Recalculate actual position value
    position_value_usd = quantity * price_usd
    position_value_gbp = position_value_usd / gbp_usd_rate

    logger.info(
        f"Position sizing: ${price_usd} x {quantity} shares = "
        f"${position_value_usd:.2f} (£{position_value_gbp:.2f})"
    )

    return quantity, position_value_usd, position_value_gbp


def validate_position(
    quantity: int,
    price_usd: float,
    gbp_usd_rate: float,
    max_position_gbp: float = MAX_POSITION_GBP,
) -> tuple[bool, str]:
    """
    Validate a proposed position against risk limits.
    Returns (is_valid, reason).
    """
    if quantity <= 0:
        return False, "Quantity must be positive"

    if price_usd <= 0:
        return False, "Price must be positive"

    position_value_gbp = (quantity * price_usd) / gbp_usd_rate

    if position_value_gbp > max_position_gbp * 1.05:  # 5% tolerance
        return False, (
            f"Position value £{position_value_gbp:.2f} exceeds "
            f"limit £{max_position_gbp:.2f}"
        )

    return True, "OK"
