"""TradingView alert webhook -> Alpaca paper-trading bridge.

Flow:  TradingView strategy alert (JSON) -> POST /webhook/<secret> -> risk checks
       -> Alpaca paper order.

SAFETY: DRY_RUN=true by default. Signals are logged, no orders are placed.
Set DRY_RUN=false in .env only when you are ready for live paper orders.

Run:   uvicorn webhook_server:app --reload --port 8080
"""

import json
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pathlib import Path

# .env lives in the project root (one level above this file),
# regardless of which directory uvicorn is started from.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MAX_NOTIONAL = float(os.environ.get("MAX_NOTIONAL_PER_TRADE", "1000"))
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "200"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

app = FastAPI(title="TV -> Alpaca bridge")


def get_client():
    """Alpaca client, paper trading only. Never touches a live account."""
    from alpaca.trading.client import TradingClient

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        raise HTTPException(500, "ALPACA_API_KEY / ALPACA_API_SECRET not set in .env")
    return TradingClient(key, secret, paper=True)


def tv_ticker_to_alpaca(ticker: str) -> str:
    """'NASDAQ:AAPL' -> 'AAPL' (Alpaca wants the bare symbol)."""
    return ticker.split(":")[-1].upper().strip()


def check_daily_loss(client) -> None:
    acct = client.get_account()
    pnl = float(acct.equity) - float(acct.last_equity)
    if pnl < -MAX_DAILY_LOSS:
        raise HTTPException(429, f"daily loss limit hit (day P&L {pnl:.2f})")


@app.get("/health")
def health():
    return {"ok": True, "dry_run": DRY_RUN}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(403, "bad secret")

    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "expected a JSON body")

    action = str(payload.get("action", "")).lower()
    ticker = tv_ticker_to_alpaca(str(payload.get("ticker", "")))
    try:
        price = float(payload.get("price", 0) or 0)
    except (TypeError, ValueError):
        price = 0

    if action not in ("buy", "sell") or not ticker or price <= 0:
        raise HTTPException(400, "payload needs action (buy/sell), ticker, price")

    log.info("signal: %s %s @ %.2f (dry_run=%s)", action, ticker, price, DRY_RUN)
    if DRY_RUN:
        return {"status": "dry_run", "action": action, "ticker": ticker}

    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = get_client()
    check_daily_loss(client)

    if action == "buy":
        qty = max(1, int(MAX_NOTIONAL // price))
        order = client.submit_order(
            MarketOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        log.info("BUY placed: %s x%d (order %s)", ticker, qty, order.id)
        return {"status": "placed", "order_id": str(order.id), "qty": qty}

    # action == "sell": close the long position instead of opening a short.
    try:
        client.get_open_position(ticker)
    except Exception:
        log.info("sell signal for %s but no open position; nothing to do", ticker)
        return {"status": "no_position", "ticker": ticker}
    client.close_position(ticker)
    log.info("SELL: closed position in %s", ticker)
    return {"status": "closed", "ticker": ticker}
