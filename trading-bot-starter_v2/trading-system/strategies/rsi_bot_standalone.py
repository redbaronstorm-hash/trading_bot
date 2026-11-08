"""Standalone RSI(2) mean-reversion bot — no TradingView needed.

This is the same strategy as strategies/rsi_mean_reversion.pine, but the
signals are computed here in Python instead of on a TradingView chart:

  1. Pull daily bars from Alpaca's market-data API
     (free plan: IEX feed, 200 calls/min — plenty for one daily check).
     Historical bars on the free plan are delayed 15 minutes, which does
     not matter for daily bars.
  2. Compute RSI(2) (Wilder's smoothing, same as Pine's ta.rsi) and the
     200-day simple moving average with pandas.
  3. Apply the rules: buy when RSI(2) < 10 and close > 200-day SMA;
     sell (close the position) when RSI(2) > 90.
  4. Route the order through the same risk controls as the webhook
     server: $1,000 max per trade, $200 daily-loss circuit breaker.

Run it once per day AFTER the market closes (4:15pm ET or later) — e.g.
with Windows Task Scheduler. Running mid-day would read today's still-
forming bar, and the signal would be based on incomplete data.

SAFETY: DRY_RUN=true by default. Decisions are logged, no orders placed.
Set DRY_RUN=false in .env only when you are ready for live paper orders.
Orders always go to the paper account (paper=True is hardcoded).

Usage (from the trading-system folder, venv active):
    python strategies/rsi_bot_standalone.py
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# .env lives in the project root, wherever this script is run from.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

SYMBOL = os.environ.get("SYMBOL", "AAPL")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
MAX_NOTIONAL = float(os.environ.get("MAX_NOTIONAL_PER_TRADE", "1000"))
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "200"))

RSI_LEN = 2
OVERSOLD = 10
OVERBOUGHT = 90
MA_LEN = 200
LOOKBACK_DAYS = 400  # > 200 trading days, so the SMA is well-formed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rsi-bot")


def get_trading_client():
    from alpaca.trading.client import TradingClient

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("ALPACA_API_KEY / ALPACA_API_SECRET not set in .env")
    return TradingClient(key, secret, paper=True)


def get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("ALPACA_API_KEY / ALPACA_API_SECRET not set in .env")
    return StockHistoricalDataClient(key, secret)


def fetch_daily_closes(symbol: str) -> pd.Series:
    """Daily closing prices, oldest -> newest, from Alpaca's data API."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = get_data_client()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS),
        feed=DataFeed.IEX,  # the feed included in Alpaca's free plan
    )
    bars = client.get_stock_bars(request)
    df = bars.df
    if df is None or df.empty:
        raise SystemExit(f"no bars returned for {symbol}")
    # single-symbol response: drop the symbol level, keep the timestamps
    closes = df["close"]
    if isinstance(closes.index, pd.MultiIndex):
        closes = closes.xs(symbol, level=0)
    closes = closes.sort_index()
    log.info("fetched %d daily bars for %s (last: %s)", len(closes),
             symbol, closes.index[-1])
    return closes


def rsi_wilder(close: pd.Series, length: int = RSI_LEN) -> pd.Series:
    """RSI with Wilder's smoothing — matches Pine Script's ta.rsi()."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length,
                        adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length,
                        adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # edge cases: no losses at all -> 100; no price movement at all -> 50
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where((avg_gain != 0) | (avg_loss != 0), 50.0)
    return rsi


def check_daily_loss(client) -> None:
    acct = client.get_account()
    pnl = float(acct.equity) - float(acct.last_equity)
    if pnl < -MAX_DAILY_LOSS:
        raise SystemExit(f"daily loss limit hit (day P&L {pnl:.2f}) — no trades")


def main() -> None:
    closes = fetch_daily_closes(SYMBOL)
    rsi = rsi_wilder(closes)
    sma = closes.rolling(MA_LEN).mean()

    price = float(closes.iloc[-1])
    r = float(rsi.iloc[-1])
    ma = float(sma.iloc[-1])
    log.info("%s: close=%.2f  RSI(%d)=%.2f  SMA(%d)=%.2f",
             SYMBOL, price, RSI_LEN, r, MA_LEN, ma)

    client = get_trading_client()
    try:
        position = client.get_open_position(SYMBOL)
        has_position = True
        log.info("open position: %s x%s", SYMBOL, position.qty)
    except Exception:
        has_position = False
        log.info("no open position in %s", SYMBOL)

    action = None
    if r < OVERSOLD and price > ma and not has_position:
        action = "buy"
    elif r > OVERBOUGHT and has_position:
        action = "sell"

    if action is None:
        log.info("no signal — holding")
        return

    log.info("SIGNAL: %s %s @ %.2f (dry_run=%s)", action, SYMBOL, price, DRY_RUN)
    if DRY_RUN:
        log.info("dry run — no order placed")
        return

    check_daily_loss(client)

    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    if action == "buy":
        qty = max(1, int(MAX_NOTIONAL // price))
        order = client.submit_order(MarketOrderRequest(
            symbol=SYMBOL, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY))
        log.info("BUY placed: %s x%d (order %s)", SYMBOL, qty, order.id)
    else:
        client.close_position(SYMBOL)
        log.info("SELL: closed position in %s", SYMBOL)


if __name__ == "__main__":
    main()
