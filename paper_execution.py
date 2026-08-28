"""Read-only Polymarket CLOB paper-fill simulator.

This module only performs public GET requests. It never loads credentials, reads a
wallet, signs typed data, or sends POST/DELETE requests to the CLOB.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

CLOB_BOOK_ENDPOINT = "https://clob.polymarket.com/book"
CLOB_FEE_RATE_ENDPOINT = "https://clob.polymarket.com/fee-rate"
MIN_PRICE_INCLUSIVE = Decimal("0.40")
MAX_PRICE_INCLUSIVE = Decimal("0.98")
# Per-user spec: every paper order is a FIXED quantity of 5 shares (the
# exchange minimum order size). No USDC-tier sizing: a $1-$3 intent often
# cannot reach min_order_size=5 at ask >= 0.20, so those orders were being
# rejected (paper_fill_rejected_below_min_order_size). The city-day total
# debit cap (20 USDC) still bounds how many 5-share intents fit per day.
TARGET_ORDER_SHARES = Decimal("5")
CITY_DAY_MAX_TOTAL_DEBIT = Decimal("20.00")
