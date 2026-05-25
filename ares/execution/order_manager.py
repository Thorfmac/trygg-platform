"""
Trygg Ares — Order Manager
Connects to TWS via ibapi, submits and monitors orders.
Manages the 15-minute execution window at US market open.

Paper trading: port 7497
Live trading:  port 7496
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TWS_HOST      = os.getenv("IBKR_TWS_HOST", "127.0.0.1")
TWS_PORT      = int(os.getenv("IBKR_TWS_PORT", "7497"))
CLIENT_ID     = int(os.getenv("IBKR_CLIENT_ID", "1"))
ACCOUNT_ID    = os.getenv("IBKR_ACCOUNT_ID", "")


class AresTWSClient(EWrapper, EClient):
    """
    TWS API client for Ares order execution.
    Inherits EWrapper (callbacks) and EClient (connection/requests).
    """

    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, self)

        self.next_order_id    = None
        self.connected        = False
        self.order_statuses   = {}  # order_id -> status
        self.errors           = []
        self._ready           = threading.Event()

    # ── Connection callbacks ──────────────────────────────────

    def connectAck(self):
        logger.info("TWS connection acknowledged")

    def nextValidId(self, orderId: int):
        """Called by TWS on connection — signals ready to trade."""
        self.next_order_id = orderId
        self.connected = True
        self._ready.set()
        logger.info(f"TWS connected — next order ID: {orderId}")

    def connectionClosed(self):
        self.connected = False
        logger.warning("TWS connection closed")

    # ── Order callbacks ───────────────────────────────────────

    def orderStatus(
        self, orderId, status, filled, remaining,
        avgFillPrice, permId, parentId, lastFillPrice,
        clientId, whyHeld, mktCapPrice
    ):
        self.order_statuses[orderId] = {
            "status":         status,
            "filled":         filled,
            "remaining":      remaining,
            "avg_fill_price": avgFillPrice,
        }
        logger.info(
            f"Order {orderId}: {status} — "
            f"filled={filled}, remaining={remaining}, "
            f"avg_price={avgFillPrice}"
        )

    def openOrder(self, orderId, contract, order, orderState):
        logger.info(f"Open order {orderId}: {contract.symbol} {order.action} {order.totalQuantity}")

    def execDetails(self, reqId, contract, execution):
        logger.info(
            f"Execution: {contract.symbol} {execution.side} "
            f"{execution.shares} @ {execution.price}"
        )

    # ── Error callbacks ───────────────────────────────────────

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Filter informational messages
        if errorCode in (2104, 2106, 2158, 2119):
            logger.debug(f"TWS info [{errorCode}]: {errorString}")
            return
        logger.error(f"TWS error [{errorCode}] reqId={reqId}: {errorString}")
        self.errors.append({"code": errorCode, "message": errorString})


def make_stock_contract(ticker: str, exchange: str = "NASDAQ", currency: str = "USD") -> Contract:
    """Create a simple equity contract."""
    contract = Contract()
    contract.symbol   = ticker
    contract.secType  = "STK"
    contract.exchange = exchange
    contract.currency = currency
    return contract


def make_market_order(action: str, quantity: int, account: str = "") -> Order:
    """
    Create a market order.
    action: 'BUY' or 'SELL'
    """
    order = Order()
    order.action          = action
    order.orderType       = "MKT"
    order.totalQuantity   = quantity
    order.tif             = "DAY"
    order.eTradeOnly      = False
    order.firmQuoteOnly   = False
    if account:
        order.account = account
    return order


def make_limit_order(action: str, quantity: int, limit_price: float, account: str = "") -> Order:
    """
    Create a limit order.
    action: 'BUY' or 'SELL'
    """
    order = Order()
    order.action          = action
    order.orderType       = "LMT"
    order.totalQuantity   = quantity
    order.lmtPrice        = round(limit_price, 2)
    order.tif             = "DAY"
    if account:
        order.account = account
    return order


class OrderExecutor:
    """
    High-level order executor.
    Manages TWS connection lifecycle and order submission.
    """

    def __init__(self):
        self.client  = None
        self._thread = None

    def connect(self, timeout: int = 15) -> bool:
        """Connect to TWS. Returns True on success."""
        self.client = AresTWSClient()
        self.client.connect(TWS_HOST, TWS_PORT, CLIENT_ID)

        # Run message loop in background thread
        self._thread = threading.Thread(
            target=self.client.run,
            daemon=True,
            name="tws-message-loop",
        )
        self._thread.start()

        # Wait for nextValidId callback (signals ready)
        ready = self.client._ready.wait(timeout=timeout)
        if not ready:
            logger.error("TWS connection timed out")
            return False

        logger.info("OrderExecutor connected to TWS")
        return True

    def disconnect(self):
        """Disconnect from TWS."""
        if self.client and self.client.connected:
            self.client.disconnect()
            logger.info("OrderExecutor disconnected from TWS")

    def submit_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
    ) -> Optional[int]:
        """
        Submit an order to TWS.
        Returns order_id on success, None on failure.
        """
        if not self.client or not self.client.connected:
            logger.error("Not connected to TWS")
            return None

        contract = make_stock_contract(ticker)

        if order_type == "LMT" and limit_price:
            order = make_limit_order(action, quantity, limit_price, ACCOUNT_ID)
        else:
            order = make_market_order(action, quantity, ACCOUNT_ID)

        order_id = self.client.next_order_id
        self.client.next_order_id += 1

        self.client.placeOrder(order_id, contract, order)
        logger.info(
            f"Order submitted: {action} {quantity} {ticker} "
            f"{order_type} — order_id={order_id}"
        )

        return order_id

    def wait_for_fill(self, order_id: int, timeout: int = 60) -> dict:
        """
        Wait for an order to fill or timeout.
        Returns final order status dict.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.client.order_statuses.get(order_id, {})
            if status.get("status") in ("Filled", "Cancelled", "Inactive"):
                return status
            time.sleep(1)

        # Return last known status on timeout
        return self.client.order_statuses.get(order_id, {"status": "Unknown"})


def test_connection() -> bool:
    """Quick connection test — use before any trading session."""
    executor = OrderExecutor()
    connected = executor.connect(timeout=10)
    if connected:
        logger.info(f"TWS connection test passed — account: {ACCOUNT_ID}")
    else:
        logger.error("TWS connection test failed")
    executor.disconnect()
    return connected


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print("Testing TWS connection...")
    result = test_connection()
    print(f"Connection test: {'PASSED' if result else 'FAILED'}")
