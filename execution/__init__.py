"""WeatherBot unified execution layer.

Paper and Live share one order model (OrderIntent), one state machine
(OrderState/OrderRecord), and one risk gate (RiskGate). Only the executor
differs: paper simulates a FAK against a fresh L2 book, live is fail-closed.
"""
from __future__ import annotations

from .order_intent import OrderIntent
from .order_state import OrderState, OrderRecord
from .risk_gate import RiskGate, RiskDecision
from .paper_executor import PaperExecutor
from .live_executor import LiveExecutor

__all__ = [
    "OrderIntent",
    "OrderState",
    "OrderRecord",
    "RiskGate",
    "RiskDecision",
    "PaperExecutor",
    "LiveExecutor",
]
