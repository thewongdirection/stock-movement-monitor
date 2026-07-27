# Stock movement monitor

Telegram alerts for unusually large trades, unusual options flow, and insider
filings on a watchlist you define. Runs as a GitHub Actions cron — no server to
keep alive.

Four independent detectors, each switchable and tunable:

| | Detector | What it catches | Data source |
|---|---|---|---|
| **L1** | `volume_anomaly` | Abnormal volume on 1/5/15-minute bars, normalised by time of day | FMP |
| **L2** | `block_trades` | Individual large prints, sized in shares | Unusual Whales |
| **L2** | `dark_pool` | Off-exchange prints, sized in dollars and % of ADV | Unusual Whales |
| **L3** | `options_flow` | Whale premium, sweeps, volume-over-open-interest | Unusual Whales |
| | `insider_trades` | SEC Form 4 purchases and sales, cluster buys | SEC EDGAR (free) |

Typical latency is one cron interval — about 5 minutes, occasionally 20 when
GitHub's scheduler is busy. Comfortably inside a 2-hour target.

---

## Three things to know before you trust the output

**1. The tape does not say who was buying.** US consolidated trade data carries
price, size, venue and condition codes — but no aggressor flag. Every "buy" or
"sell" label in this project is *inferred*, by comparing the print price to the
prevailing bid/ask (the Lee-Ready quote rule). Alerts say so explicitly, and
say "side undetermined" when there isn't enough quote context to guess. Treat
any tool that states a side as fact with suspicion.

**2. Insider alerts are fast, but the trade is not fresh.** Form 4 is due within
**two business days of the transaction**. A perfect pipeline still reports a
trade that happened days ago. Every insider alert shows both the trade date and
the filing date so the lag is visible. What this gets you is being among the
first to know a filing landed — not being early to the trade.

**3. Dark pool prints don't name the venue.** Off-exchange trades reach the tape
through a FINRA reporting facility. You learn size, price and that it was
off-exchange. You do not learn *which* pool — FINRA publishes per-ATS detail
weekly, with a multi-week lag.

---

## Setup

### 1. Telegram bot

Message [@BotFather](https://t.me/botfather) → `/newbot` → copy the token.
Then send your new bot any message, and:

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=123456:ABC...
PYTHONPATH=src python -m monitor telegram-chat-id
```

That prints the `TELEGRAM_CHAT_ID` to use. (For a group, add the bot to the
group and post there instead.)

### 2. API keys

| Secret | Needed for | Where |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | delivery | @BotFather |
| `TELEGRAM_CHAT_ID` | delivery | the command above |
| `UW_API_KEY` | L2 + L3 | [unusualwhales.com/settings/api-dashboard](https://unusualwhales.com/settings/api-dashboard) |
| `FMP_API_KEY` | L1, and ADV for L2 | [financialmodelingprep.com](https://financialmodelingprep.com/developer) |
| `SEC_USER_AGENT` | insider filings | your own string, e.g. `stock-monitor you@yourdomain.com` |

`SEC_USER_AGENT` must contain a contact address that actually reaches you —
SEC's fair-access policy requires it, and the monitor refuses to call EDGAR
with the shipped example value.

If you only want insider alerts, you need no paid keys at all: set
`enabled: false` on the other four detectors.

Add all five under **Settings → Secrets and variables → Actions**.

### 3. Watchlist

Edit `config.yaml` — `tickers` at the top. Then:

```bash
PYTHONPATH=src python -m monitor validate   # config is sane
PYTHONPATH=src python -m monitor verify     # every endpoint actually answers
PYTHONPATH=src python -m monitor test-alert # a message arrives in Telegram
```

**Run `verify` before trusting the cron.** Unusual Whales serves 403 to
unauthenticated requests — including on their own API documentation — so the
endpoint paths this project ships with could not be confirmed against their
spec while it was written. They are therefore **config values, not hardcoded**:

```yaml
providers:
  unusual_whales:
    paths:
      dark_pool_ticker: /api/darkpool/{ticker}
      flow_alerts: /api/option-trades/flow-alerts
      # ...
```

`verify` probes each one with your key and prints which answered, which 404'd,
and the field names in the response. If a path has moved, fix it here — no code
change needed. The field extraction is likewise tolerant of renames: a changed
key costs one attribute on the alert, never the whole row.

### 4. Turn it on

The workflow runs every 5 minutes once merged to your default branch. Trigger a
manual run from the Actions tab (with the dry-run box ticked) to see it work.

---

## Tuning

Defaults follow the conventions market analysts use. Every threshold is
overridable within a bounded range — `monitor explain` prints the full
reference, and out-of-range values are **clamped with a warning** at run time
(reported in a Telegram footer) while `monitor validate` treats the same value
as a hard error. So a fat-fingered config degrades rather than silently killing
your alerts, and CI still catches it.

### Why these defaults

| Setting | Default | Reasoning |
|---|---|---|
| `rvol_threshold` | 2.0 | RVOL ≥ 2 is the standard "unusual volume" line; ≥ 5 is extreme |
| `zscore_threshold` | 3.0 | 3σ is the conventional outlier cut |
| `combine` | `all` | Requiring both tests is dramatically quieter than either alone |
| `baseline_sessions` | 20 | Matches the 20-day average volume analysts quote |
| `block_trades.preset` | `institutional` | The textbook block (10k shares / $200k) is trivial in a 2026 mega-cap — that's the `classic` preset if you want it |
| `dark_pool.min_pct_of_adv` | 0.5% | Size relative to normal liquidity is what matters, not absolute size |
| `options_flow.min_premium` | $100k | The conventional floor for "whale" flow |
| `require_volume_gt_oi` | `true` | Volume above open interest implies a *newly opened* position — the single most useful unusual-flow filter |
| `exclude_deep_itm_pct` | 20% | Deep ITM size is often a stock substitute or assignment mechanic, not a bet |
| `max_dte` | 365 | Long-dated LEAPS flow is usually hedging |
| `insider.min_notional_purchase` | $100k | Insiders buying spend their own money — there's one reason to do it |
| `insider.min_notional_sale` | $500k | 5× the purchase floor, because selling is far noisier (diversification, taxes, a house) |
| `exclude_10b5_1_sales` | `true` | Pre-arranged plan sales were decided months earlier |
| `cluster_min_insiders` | 2 | Several insiders buying independently is the strongest documented insider signal — these escalate to high severity |
| `include_derivative` | `false` | Option exercises and grants are compensation mechanics, not decisions |

### The noise problem, and what's done about it

A naive volume detector fires at every open, every close and every earnings
day. Countermeasures, all tunable:

- **Time-of-day normalisation.** Each bar is compared only against the same
  clock minute on previous sessions. Intraday volume is famously U-shaped;
  normalising that away is the single biggest noise reduction available. A slot
  that is *always* busy never alerts.
- **`warmup_minutes`** skips the opening auction distortion.
- **`cooldown_minutes`** silences a ticker after it fires, so one busy session
  can't produce forty alerts.
- **`min_bar_notional`** keeps thin names from tripping on statistical noise.
- **`max_alerts_per_run`**, per detector and globally, as a circuit breaker.
- **Severity is graded on RVOL, not the z-score.** In a name with very steady
  volume the standard deviation is tiny, so even a mild spike scores a huge
  z-score and would pin everything to HIGH. RVOL is a ratio and stays
  comparable across tickers.

Low-severity alerts are delivered with notifications suppressed — they sit in
the chat for review without buzzing your phone.

### Per-ticker overrides

```yaml
overrides:
  TSLA:
    volume_anomaly:
      rvol_threshold: 3.0    # habitually busy, needs a higher bar
    dark_pool:
      min_notional: 5000000
```

### A note on the two L2 detectors

With Unusual Whales as the trades provider, `block_trades` and `dark_pool` read
the **same** off-exchange print stream and differ only in how they threshold it
— shares versus dollars-and-%ADV. They cooperate so one print can never produce
two alerts, but you'll get the cleanest results enabling one. `dark_pool` is on
by default; `block_trades` is off.

---

## Commands

```
monitor run                 one polling cycle (what the cron calls)
  --dry-run                 print alerts instead of sending; leaves state untouched
  --force                   run session-bound detectors even when the market is closed
monitor validate            strict config check — exits 1 on any problem
monitor verify              live probe of every provider endpoint
monitor explain [detector]  thresholds, defaults, bounds and rationale
monitor test-alert          send a sample alert through Telegram
monitor telegram-chat-id    look up your chat id during setup
monitor state               what the state file currently holds
```

---

## How it works

```
cron (5 min)
  └─ market-hours gate ─── per detector, not globally:
     │                     Form 4 filings arrive until ~22:00 ET, so the
     │                     insider detector runs whenever the cron fires
     ├─ fetch per ticker ── bars, prints, flow, filings (once each, shared)
     ├─ detectors ───────── each returns candidate alerts
     ├─ dedup + severity ── SQLite; capped; sorted most-severe first
     └─ Telegram ────────── one message per alert
```

State (dedup keys, read watermarks, cooldowns) lives in SQLite, kept in the
Actions cache between runs. **Losing that cache is safe:** with no watermark a
detector falls back to `run.cold_start_lookback_minutes` (30 by default) rather
than replaying a whole day of alerts at you.

Two deliberate reliability choices:

- **Progress is only recorded for alerts that were actually delivered.** If
  Telegram is down, the watermark doesn't advance and the alert is re-sent next
  run. The dedup table stops a duplicate once delivery recovers.
- **One ticker's failure never ends the run.** A provider error is collected and
  reported; the other tickers still get processed. Setup failures (a missing
  key) are reported once per run rather than once per symbol.

`keepalive.yml` commits a weekly heartbeat, because GitHub disables scheduled
workflows in a repository with no activity for 60 days.

---

## Known limitations

- **Unusual Whales endpoint paths are unverified** against their spec, for the
  403 reason above. `monitor verify` is how you confirm them. Same for which FMP
  URL generation your plan can reach — the provider tries each known shape and
  remembers the one that answers.
- **The volume baseline needs intraday history.** Roughly 20 sessions of bars.
  If your FMP plan caps intraday history shorter than that, `verify` will warn
  and the L1 detector will stay quiet rather than judge on a thin baseline.
- **GitHub's cron is best-effort**, often 5-20 minutes late. Fine for a 2-hour
  target; if you ever need sub-second alerting on individual prints, that means
  an always-on process holding a websocket, not this design.
- **The market calendar is hardcoded** through 2028 (`market_calendar.py`). Past
  that it falls back to weekday logic and says so in the run log — it fails open
  rather than deciding the market is shut.
- **Not investment advice, and not a trading signal.** Unusual size is a
  prompt to go look, nothing more.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 159 tests, no network needed
```

Tests run against fixtures throughout — provider payload parsing, the detection
logic, config bounds, state transitions and the full engine pipeline with stub
providers.
