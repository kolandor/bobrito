"""Binance REST broker for Testnet and Live modes.

Uses the official `binance-connector` library for signed REST calls.
Testnet and Live are selected by the base_url passed at construction.

Safety: Live trading requires `live_trading_enabled=True` in settings.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from bobrito.execution.base import (
    BrokerBase,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    SymbolFilters,
)
from bobrito.monitoring.logger import get_logger
from bobrito.monitoring.metrics import MetricsCollector

log = get_logger("execution.binance")


class BinanceBroker(BrokerBase):
    """Signed REST broker for Binance Spot (Testnet or Live)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        mode: str = "testnet",
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        self._mode = mode
        self._filters: dict[str, SymbolFilters] = {}
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"X-MBX-APIKEY": self._key},
            timeout=10.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ── BrokerBase ────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
    async def place_order(self, request: OrderRequest) -> OrderResult:
        if not request.client_order_id:
            request.client_order_id = str(uuid.uuid4()).replace("-", "")[:36]

        filters = await self.get_symbol_filters(request.symbol)
        if filters is None:
            raise RuntimeError("Symbol filters unavailable; cannot place order safely")

        qty_str = str(filters.quantize_qty(request.quantity))
        params: dict = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": qty_str,
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "FULL",
        }
        if request.order_type == OrderType.LIMIT and request.price:
            params["price"] = str(filters.quantize_price(request.price))
            params["timeInForce"] = "GTC"

        data = await self._signed_post("/api/v3/order", params)

        if "code" in data:
            log.error(f"Order rejected: {data}")
            MetricsCollector.order_rejections_total.labels(reason=str(data.get("code"))).inc()
            return OrderResult(
                client_order_id=request.client_order_id,
                exchange_order_id="",
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                status=OrderStatus.REJECTED,
                requested_qty=request.quantity,
            )

        fills = data.get("fills", [])
        total_qty = sum(float(f["qty"]) for f in fills)
        total_notional = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_price = total_notional / total_qty if total_qty else 0.0
        commission = sum(float(f["commission"]) for f in fills)
        commission_asset = fills[0]["commissionAsset"] if fills else "USDT"

        result = OrderResult(
            client_order_id=data.get("clientOrderId", request.client_order_id),
            exchange_order_id=str(data.get("orderId", "")),
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status=_map_status(data.get("status", "")),
            requested_qty=request.quantity,
            filled_qty=float(data.get("executedQty", total_qty)),
            average_price=avg_price,
            commission=commission,
            commission_asset=commission_asset,
            timestamp=datetime.utcfromtimestamp(data.get("transactTime", 0) / 1000),
            raw=data,
        )
        MetricsCollector.trades_total.labels(side=request.side.value, mode=self._mode).inc()
        log.info(
            f"[{self._mode.upper()}] {request.side.value} {result.filled_qty:.6f} BTC "
            f"@ {result.average_price:.2f} | status={result.status}"
        )
        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        params = {"symbol": symbol, "origClientOrderId": order_id}
        data = await self._signed_delete("/api/v3/order", params)
        return "orderId" in data

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
    async def get_order(self, symbol: str, order_id: str) -> OrderResult | None:
        params = {"symbol": symbol, "origClientOrderId": order_id}
        data = await self._signed_get("/api/v3/order", params)
        if "code" in data:
            return None
        return OrderResult(
            client_order_id=data.get("clientOrderId", ""),
            exchange_order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=OrderSide(data.get("side", "BUY")),
            order_type=OrderType(data.get("type", "MARKET")),
            status=_map_status(data.get("status", "")),
            requested_qty=float(data.get("origQty", 0)),
            filled_qty=float(data.get("executedQty", 0)),
            average_price=float(data.get("price", 0)),
            raw=data,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
    async def get_balances(self) -> dict[str, float]:
        data = await self._signed_get("/api/v3/account", {})
        balances = {}
        for b in data.get("balances", []):
            asset = b["asset"]
            if asset in ("BTC", "USDT"):
                balances[asset] = float(b["free"])
        return balances

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
    async def get_symbol_filters(self, symbol: str) -> SymbolFilters | None:
        if symbol in self._filters:
            return self._filters[symbol]
        resp = await self._client.get("/api/v3/exchangeInfo", params={"symbol": symbol})
        if resp.is_error:
            return None
        info = resp.json()
        step_size = Decimal("0.00001")
        min_qty = Decimal("0.00001")
        min_notional = Decimal("5.0")
        tick_size = Decimal("0.01")
        for sym_info in info.get("symbols", []):
            if sym_info["symbol"] == symbol:
                for f in sym_info.get("filters", []):
                    ft = f.get("filterType", "")
                    if ft == "LOT_SIZE":
                        step_size = Decimal(str(f["stepSize"]))
                        min_qty = Decimal(str(f["minQty"]))
                    elif ft == "NOTIONAL":
                        min_notional = Decimal(str(f.get("minNotional", 5.0)))
                    elif ft == "MIN_NOTIONAL":
                        min_notional = Decimal(str(f.get("minNotional", 5.0)))
                    elif ft == "PRICE_FILTER":
                        tick_size = Decimal(str(f["tickSize"]))
                break
        filters = SymbolFilters(
            symbol=symbol,
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
            tick_size=tick_size,
        )
        self._filters[symbol] = filters
        return filters

    # ── Signed request helpers ─────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        sig = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    async def _signed_get(self, path: str, params: dict) -> dict:
        signed = self._sign(dict(params))
        resp = await self._client.get(path, params=signed)
        MetricsCollector.api_errors_total.labels(endpoint=path).inc() if resp.is_error else None
        return resp.json()

    async def _signed_post(self, path: str, params: dict) -> dict:
        signed = self._sign(dict(params))
        resp = await self._client.post(path, data=signed)
        if resp.is_error:
            MetricsCollector.api_errors_total.labels(endpoint=path).inc()
        return resp.json()

    async def _signed_delete(self, path: str, params: dict) -> dict:
        signed = self._sign(dict(params))
        resp = await self._client.delete(path, params=signed)
        return resp.json()


def _map_status(status: str) -> OrderStatus:
    mapping = {
        "NEW": OrderStatus.OPEN,
        "PARTIALLY_FILLED": OrderStatus.OPEN,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELLED,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.CANCELLED,
    }
    return mapping.get(status, OrderStatus.PENDING)
