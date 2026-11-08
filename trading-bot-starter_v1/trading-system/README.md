# TradingView → Alpaca paper-trading bot (starter)

A minimal algorithmic trading loop you can actually run:

```
TradingView (Pine Script strategy)
        │  alert webhook (JSON)
        ▼
FastAPI server (this repo) — validates secret, enforces risk limits
        │  Alpaca API
        ▼
Alpaca paper trading account (fake money, real market data)
```

## Setup (VS Code)

**1. Install dependencies**

Open this folder in VS Code, then in the terminal:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Get free Alpaca paper-trading keys**

- Sign up at alpaca.markets → switch to **Paper Trading** → generate an API key + secret.
- `cp .env.example .env` and paste them in. Invent a long `WEBHOOK_SECRET`.

**3. Run the server**

```bash
cd server
uvicorn webhook_server:app --reload --port 8080
```

Check `http://localhost:8080/health` → `{"ok": true, ...}`.

**4. Expose it to the internet (TradingView can't reach localhost)**

```bash
ngrok http 8080
```

Your webhook URL becomes:

```
https://<your-ngrok-id>.ngrok.io/webhook/<your WEBHOOK_SECRET>
```

**5. Wire up TradingView**

- Paste `strategies/rsi_mean_reversion.pine` into TradingView's Pine Editor → **Add to chart**.
- Check the **Strategy Tester** tab — backtest first, on out-of-sample dates.
- Right-click the chart → **Add alert** → Condition: your strategy → enable **Webhook URL** → paste the URL from step 4.
- The strategy's `alert_message` already sends `{"action":"buy","ticker":"...","price":...}` JSON.

**6. Watch it work (safely)**

`DRY_RUN=true` is the default: signals get logged, no orders placed. When the logs
look right, flip `DRY_RUN=false` in `.env` and restart — orders go to your **paper**
account only. This code can never touch a live brokerage account (`paper=True` is hardcoded).

## Risk controls (built in)

- Max notional per trade (`MAX_NOTIONAL_PER_TRADE`)
- Daily loss circuit breaker (`MAX_DAILY_LOSS`) — server refuses new orders past it
- Sells close the position instead of opening a short

## What to learn next

- Walk-forward testing: optimize on 2022–2023, validate on 2024–2025. If it only works in-sample, it's curve-fit.
- Slippage + commissions are already in the Pine Script — most beginner strategies die here.
- When you're ready for real automation beyond one strategy, the webhook server is the seam to extend: add position sizing, multi-strategy routing, a trade journal DB.
