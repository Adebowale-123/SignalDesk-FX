# SignalDesk FX

A private portal that tells you and your team **when to trade and when to wait** on the major forex pairs and gold.
It analyses the market, then shows **TRADE NOW** (with entry, stop loss, take profit and the reasons) or **WAIT** (with the reason).

It doesn't place trades or connect to a broker, and it holds no money. It only supports the decision.

## What it does

1. **Downloads prices.** It uses closed candles only. The default source is Yahoo (free, no key). Twelve Data and OANDA practice are optional.
2. **Analyses each pair** on the timeframes you choose:
   - higher-timeframe trend agreement
   - entry trend (EMAs)
   - momentum (RSI, MACD)
   - a setup: a pullback to the fast EMA, or a 20-candle breakout
   - market condition: ADX, volatility and the trading session
3. **Scores the setup out of 100.** It says TRADE NOW only if the score reaches your minimum and none of the hard WAIT rules apply:
   - the market is closed
   - outside your sessions
   - the higher timeframes disagree
   - the market is ranging
   - RSI is stretched
   - there is a volatility spike
4. **Sets stop loss and take profit.** The stop goes beyond the last swing point and is never tighter than 1 ATR. Take profit is your risk-reward ratio times the risk.
5. **Shows confidence from real past results.** Confidence is the percentage of similar-scoring setups that reached take profit before stop loss in the latest backtest. Until a backtest has run, the board shows the score instead.
6. **Tracks every signal** to TP, SL or expiry (Track record page).
7. **Sends alerts** by Telegram and/or email.

## Quick start (Windows)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_portal          # 8 instruments + 2 strategies
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000 and log in. In a second terminal, keep the engine running:

```powershell
.venv\Scripts\python manage.py run_engine           # analyses every few minutes (Settings)
.venv\Scripts\python manage.py backtest 1           # calibrate confidence for strategy #1
```

## Setting it up yourself (Settings page, admins only)

| What | Where |
|---|---|
| Pairs on/off | Settings → Pairs |
| Entry timeframe (5m to 1d), confirmation timeframes, sessions, risk-reward, stop method, minimum score, indicator lengths | Settings → Strategies → edit or **New strategy** |
| Data source and keys | Settings → Data source |
| Telegram bot token and chat ID, alert emails, **Send test alert** | Settings → Alerts |

After changing a strategy, run its backtest (Backtests page) so its confidence matches the new rules.

Team members only see the board, pair pages, track record and backtests. Add them in `/admin/` → Users. Leave "staff" unticked so they can't change settings.

## Telegram alerts

1. In Telegram, message **@BotFather** and send `/newbot`. Copy the token it gives you.
2. Add the bot to your team group (or message it yourself), then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id`.
3. Paste both into Settings → Alerts, save, then click **Send test alert**.

## Reading the backtest honestly

With 1:2 risk-reward, a strategy needs to win more than **33%** of trades to break even, and a little more once spread is counted.
The default rules are textbook trend-following, and in testing on Yahoo data they showed **no edge** after spread:

| Strategy | Trades | Win rate | Avg R | Profit factor |
|---|---|---|---|---|
| Intraday (15m entry, 1h + 4h) | 439 | 31.1% | −0.15 | 0.79 |
| Swing (1h entry, 4h + 1d) | 1,487 | 31.1% | −0.11 | 0.85 |

Treat the portal as a measuring tool. Change one setting at a time, re-run the backtest, and only trust a strategy that keeps a profit factor above about 1.2 over many trades.

## Not connected yet (phase 2)

- Economic calendar (block signals around high-impact news). Until then, check the calendar yourself before trading.
- Fundamental and sentiment data.
- AI-written explanations.

## Deploying on Render (free)

1. Render dashboard → **New → Blueprint** → pick this repo. It reads `render.yaml`, which creates a web service and a PostgreSQL database.
2. When asked, type **ADMIN_USERNAME** and **ADMIN_PASSWORD**. That becomes your first login.
3. After it's live, log in. Go to **Backtests → Run backtest** for each strategy you use; this calibrates confidence.
4. **Keep it running 24/7.** Free services sleep after 15 minutes without visitors, and the analysis stops while it sleeps. To prevent this:
   - Copy `ENGINE_TICK_KEY` from Render → Environment.
   - Create a free job at [cron-job.org](https://cron-job.org) that opens `https://<your-app>.onrender.com/engine/tick/?key=<ENGINE_TICK_KEY>` every 5 minutes. This keeps the service awake and runs the analysis.

Notes:
- Render's free PostgreSQL expires after 30 days unless upgraded.
- If Yahoo blocks the server, switch to Twelve Data in Settings → Data source. It needs a free key from twelvedata.com.

## Tests

```powershell
.venv\Scripts\python manage.py test apps
```

The tests cover:
- indicator causality (no look-ahead)
- higher-timeframe alignment
- market hours
- TP/SL outcome rules
- stop-loss and take-profit levels
- confidence calibration
- the backtest
- private access
- the strategy settings form
