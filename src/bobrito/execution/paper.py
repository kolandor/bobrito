"""Paper Trading Broker.

Simulates order execution against real market data with configurable:
  - taker fee (default 0.1%)
  - slippage (default 5 bps)
  - initial USDT balance (default 200 USDT)

No real orders are placed. All state is in-memory.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from bobrito.execution.base import (
    BrokerBase,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from bobrito.monitoring.logger import get_logger
from bobrito.monitoring.metrics import MetricsCollector

log = get_logger("execution.paper")

# Default symbol filters for BTCUSDT
_DEFAULT_FILTERS = {
    "step_size": 0.00001,
    "min_qty": 0.00001,
    "min_notional": 5.0,
}


class PaperBroker(BrokerBase):
    def __init__(
        self,
        initial_usdt: float = 200.0,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
    ) -> None:
        self._fee_rate = fee_rate
        self._slippage_bps = slippage_bps

        # Internal balances: free + locked per asset
        self._balances: dict[str, dict[str, float]] = {
            "USDT": {"free": initial_usdt, "locked": 0.0},
            "BTC": {"free": 0.0, "locked": 0.0},
        }
        self._orders: dict[str, OrderResult] = {}
        self._last_price: float = 0.0

    # ── Public price update (called by feed) ──────────────────────────────

    def update_price(self, price: float) -> None:
        self._last_price = price

    # ── BrokerBase implementation ─────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> OrderResult:
        if not request.client_order_id:
            request.client_order_id = str(uuid.uuid4())

        fill_price = self._apply_slippage(self._last_price, request.side)

        if request.order_type == OrderType.LIMIT and request.price:
            fill_price = request.price

        commission = request.quantity * fill_price * self._fee_rate
        commission_asset = "USDT"

        result = OrderResult(
            client_order_id=request.client_order_id,
            exchange_order_id=f"PAPER-{uuid.uuid4().hex[:12]}",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status=OrderStatus.FILLED,
            requested_qty=request.quantity,
            filled_qty=request.quantity,
            average_price=fill_price,
            commission=commission,
            commission_asset=commission_asset,
            timestamp=datetime.utcnow(),
        )

        self._orders[result.client_order_id] = result
        self._apply_fill(result)

        log.info(
            f"[PAPER] {request.side.value} {request.quantity:.6f} BTC "
            f"@ {fill_price:.2f} | fee={commission:.4f} USDT"
        )
        MetricsCollector.trades_total.labels(side=request.side.value, mode="paper").inc()
        return result

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED
            return True
        return False

    async def get_order(self, symbol: str, order_id: str) -> OrderResult | None:
        return self._orders.get(order_id)

    async def get_balances(self) -> dict[str, float]:
        return {
            asset: data["free"]
            for asset, data in self._balances.items()
        }

    async def get_symbol_filters(self, symbol: str) -> dict:
        return dict(_DEFAULT_FILTERS)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        slip = price * self._slippage_bps / 10_000
        return price + slip if side == OrderSide.BUY else price - slip

    def _apply_fill(self, result: OrderResult) -> None:
        notional = result.filled_qty * result.average_price
        commission = result.commission

        if result.side == OrderSide.BUY:
            # Debit USDT, credit BTC
            self._balances["USDT"]["free"] -= notional + commission
            self._balances["BTC"]["free"] += result.filled_qty
        else:
            # Credit USDT, debit BTC
            self._balances["BTC"]["free"] -= result.filled_qty
            self._balances["USDT"]["free"] += notional - commission

    def restore_balances(self, free_usdt: float, free_btc: float) -> None:
        """Overwrite balances with values reconstructed from the database.

        Called once at bot startup in paper mode so the simulated account
        always reflects the true state regardless of how many restarts
        have occurred.
        """
        self._balances["USDT"]["free"] = max(free_usdt, 0.0)
        self._balances["BTC"]["free"] = max(free_btc, 0.0)
        log.info(
            f"[PAPER] Balances restored from DB: "
            f"USDT={free_usdt:.2f} BTC={free_btc:.6f}"
        )

    def get_full_balances(self) -> dict:
        return {k: dict(v) for k, v in self._balances.items()}
