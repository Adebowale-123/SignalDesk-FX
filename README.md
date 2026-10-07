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
8. **Avoids news, reads fundamentals and sentiment, re-tests itself and discovers new strategies automatically** (see below).

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

## Automatic news filter

- Every hour the portal downloads the free ForexFactory economic calendar.
- A pair says **WAIT** from 30 minutes before to 30 minutes after high-impact news for either of its currencies. Gold follows USD news.
- The window, and whether medium-impact news also counts, are set per strategy.
- The board lists the next 24 hours of relevant news.

## Self-tuning (automatic)

Once a week (configurable per strategy), each active strategy re-tests itself:

1. It downloads fresh history and replays about 1,000 variations of its settings: minimum score, risk : reward, stop loss method, ADX threshold and sessions.
2. It picks the variation that did best on the **older 70%** of the data.
3. It adopts that variation **only if** it is also profitable on the **newest 30%**, which played no part in choosing it. That means at least 20 trades, profit factor of at least 1.1, and a better result than the current settings. Otherwise it keeps the current settings, and if those don't pass either, the board warns that there's **no proven edge**.
4. Confidence is recalibrated with a fresh backtest.

Every self-test is logged on the Backtests page, with before/after settings and an **Undo** button. **Tune now** runs one on demand.

Self-tuning only adjusts those settings within fixed rules. It can't invent new strategies, and a strategy that passes is still not guaranteed to keep working. Trust it only after it also holds up on the Track record page.

## Fundamentals and sentiment (automatic)

Every few hours it downloads free data, with no keys needed:

- **Fundamentals:** dollar index trend and US 10-year yield direction (Yahoo). These are the main USD drivers; gold moves the opposite way. Oil drives CAD.
- **Sentiment, risk mood:** S&P 500 trend and VIX stress. Risk-on favours AUD, NZD and CAD; risk-off favours JPY, CHF, USD and gold.
- **Sentiment, positioning:** the weekly CFTC Commitments of Traders report shows how large speculators are positioned in each currency and in gold. A 3-year extreme is flagged as a **crowded trade**.

Each pair's "Why?" list shows both. Per strategy you choose whether they only inform, block opposing trades, or are required to agree, and whether to skip crowded trades. Self-tuning tests these options too. Values are used only after they were published (daily closes the next day, COT from the Saturday after), so backtests can't peek ahead. The **Research** page shows today's picture.

## Strategy discovery (automatic)

About once a month (Settings → Strategy discovery) the system builds new strategies itself. It combines:

- 4 entry setups: pullback, breakout, RSI turn, MACD turn
- 4 timeframe families
- the fundamental/sentiment filters and the crowded-trade rule
- score, risk : reward, stop, ADX and session choices

That's about 15,500 combinations. A new strategy is created **only if** the best combination for a family is also profitable on recent data it wasn't chosen on: at least 30 trades, profit factor of at least 1.2. Passing strategies are switched on (up to a limit) and marked **Auto**. They're re-tested weekly by self-tuning and **switched off automatically** if they stop working. On Render's free server a search can take about an hour. If it's cut off, it retries automatically.

Still not connected: AI-written explanations.

## Auto-trading (OANDA)

When switched on, every fresh TRADE NOW signal is traded on your OANDA account:

1. **Checks run first.** It skips a signal (and records why) if:
   - the pair already has an open trade, or the maximum number of open trades is reached;
   - high-impact news is close;
   - price has moved too far from the signal's entry (default: more than a quarter of the stop distance);
   - confidence is below your minimum;
   - a daily or weekly loss limit has paused trading.
2. **The position is sized** so that hitting the stop loss loses about your **risk %** (default 1%).
3. **A market order is placed with the stop loss and take profit attached at OANDA.** They work even if this server is down.
4. **Trades close at take profit or stop loss by themselves.** If its signal expires first, it is closed at market (like the backtest). Then the system waits for the next signal.
5. **Telegram/email messages** go out for every trade opened, closed, rejected or paused.

**Off-switch:** **Switch OFF** stops new trades. **Stop and close all** also closes every open trade.

**Requirements:**
- OANDA must also be the price source, so signal levels match the prices it trades at.
- A LIVE (real money) account needs an extra explicit permission tick.
- Switch auto-trading on in **one place only** (Render, not also a local copy).

**Setup:**
1. Open an OANDA demo account and generate an API token (OANDA → Manage API Access).
2. Go to Settings → Data source: choose OANDA, paste the token, account type **Practice**.
3. Go to Auto-trading → **Test connection** (it finds your account ID) → review the risk rules → **Switch ON**.

## Deploying on Render (free)

1. Create a free PostgreSQL database at [neon.tech](https://neon.tech) and copy its connection string (`postgresql://...`). A Render workspace only gets one free database, and Neon's free tier doesn't expire.
2. Render dashboard → **New → Blueprint** → pick this repo. It reads `render.yaml`, which creates the web service.
3. When asked, paste the Neon string as **DATABASE_URL**, and type **ADMIN_USERNAME** and **ADMIN_PASSWORD**. That becomes your first login.
4. After it's live, log in. Go to **Backtests → Run backtest** for each strategy you use; this calibrates confidence.
5. **Keep it running 24/7.** Free services sleep after 15 minutes without visitors, and the analysis stops while it sleeps. To prevent this:
   - Copy `ENGINE_TICK_KEY` from Render → Environment.
   - Create a free job at [cron-job.org](https://cron-job.org) that opens `https://<your-app>.onrender.com/engine/tick/?key=<ENGINE_TICK_KEY>` every 5 minutes. This keeps the service awake and runs the analysis.

Notes:
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
