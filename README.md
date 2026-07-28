# Stock movement monitor

Telegram alerts for unusually large trades, unusual options flow, and insider
filings on a watchlist you define. Runs as a GitHub Actions cron — no server to
keep alive.

Six independent detectors, each switchable and tunable:

| | Detector | What it catches | Data source |
|---|---|---|---|
| **L1** | `volume_anomaly` | Abnormal volume on 30s/1/5/15/30-minute bars, normalised by time of day | FMP, IBKR, or a replay file |
| **L2** | `block_trades` | Individual large prints, sized in shares | Unusual Whales |
| **L2** | `dark_pool` | Off-exchange prints, sized in dollars and % of ADV | Unusual Whales |
| **L3** | `options_flow` | Whale premium, sweeps, volume-over-open-interest | Unusual Whales |
| **L3-lite** | `option_volume` | Whole-chain option volume vs its average | IBKR (no options feed needed) |
| | `insider_trades` | SEC Form 4 purchases and sales, cluster buys | SEC EDGAR (free) |

Typical latency is one cron interval — about 5 minutes, occasionally 20 when
GitHub's scheduler is busy. Comfortably inside a 2-hour target.

Every alert can carry a CAN SLIM scorecard as a PDF, scored against the
[can-slim-grader](https://github.com/thewongdirection/can-slim-grader) rubric —
programmatically by default, or with Claude writing the judgement half if you
[turn the narrator on](#the-narrator-optional).

You control it from Telegram — list and edit the watchlist, pull a ticker's
14-day signal history, request a CAN SLIM scorecard, and change any threshold or
switch any level on and off. **`monitor console` gives you that whole surface
locally, with no bot token**, so you can try it before setting anything up.

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

## Try it without Telegram

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m monitor console
```

Same router, same handlers, same replies as the bot — only the transport
differs. Config edits are real and persist. Buttons become numbered choices.

```
> list                      watchlist with 14-day signal counts
> add PLTR CRWD             start watching
> levels                    which detection levels are on
> set dark_pool min_notional 2.5M
> set TSLA volume_anomaly rvol_threshold 4
> config volume_anomaly     current thresholds and their allowed ranges
> explain volume_anomaly    what each setting does and why
> history NVDA              signals in the last 14 days
> grade NVDA                CAN SLIM scorecard + PDF
> changes                   what differs from config.yaml
> status                    market state, health, last run
```

Or non-interactively: `monitor console --script "list; add PLTR; levels"`.

## Setup

> **[SETUP.md](SETUP.md) is the step-by-step walkthrough** — nine numbered steps
> from `git clone` to alerts on your phone, each with a command that checks it
> worked. The reference below covers the same ground by topic; start there if you
> would rather be told what to type next.

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
| `FMP_API_KEY` | L1, and ADV for L2 — needs FMP's **Starter** plan or above | [financialmodelingprep.com](https://financialmodelingprep.com/developer) |
| `SEC_USER_AGENT` | insider filings | your own string, e.g. `stock-monitor you@yourdomain.com` |

`SEC_USER_AGENT` must contain a contact address that actually reaches you —
SEC's fair-access policy requires it, and the monitor refuses to call EDGAR
with the shipped example value.

If you only want insider alerts, you need no paid keys at all: set
`enabled: false` on every other detector. That is the only genuinely free
configuration — FMP's free tier authenticates but 403s on the price-history
endpoints L1 and CAN SLIM need, which `monitor verify` reports as a missing
entitlement rather than a bad key.

Add all five under **Settings → Secrets and variables → Actions**.

### 2b. Optional: CAN SLIM scorecards

Alerts can carry a CAN SLIM PDF for the name that triggered them:

```bash
git clone --depth 1 https://github.com/thewongdirection/can-slim-grader vendor/can-slim-grader
```

Read [what this actually grades](#can-slim-scorecards) before relying on it —
the letters are scored programmatically against the skill's published rubric,
which is not the same thing as the skill's own agent judgement.

Optionally, Claude can supply the judgement half — the per-letter commentary,
**N**'s "new" story and **I**'s sponsorship quality:

```bash
pip install -r requirements-narrator.txt   # the anthropic SDK
export ANTHROPIC_API_KEY=sk-ant-...        # or the ANTHROPIC_API_KEY secret in CI
```

Then `narrator: llm` under `canslim:` in `config.yaml`, or `/narrator llm` from
the bot. It cannot change a computed letter — see
[the narrator](#the-narrator-optional).

### 2c. Optional: Interactive Brokers

IBKR buys you finer bars (30-second), a genuine 90-day *dollar* ADV, and the
`option_volume` signal. What it does **not** buy is individual block or dark-pool
prints — see [IBKR's limits](#what-ibkr-can-and-cannot-do).

The catch is operational: **IBKR has no API key.** It needs a Client Portal
Gateway running and interactively logged in, with a session that expires roughly
daily. That works on a machine you control. It cannot work on a GitHub Actions
runner, so on the cron leave `providers.ibkr.base_url` empty and use FMP.

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

## Controlling it from Telegram

`monitor bot` runs a long-polling bot. Long polling rather than webhooks because
webhooks need a public HTTPS endpoint, and the whole premise here is not running
a server. The consequence: **the cron sends alerts on its own, but interactive
commands only work while `monitor bot` is running.** Start it when you want to
change something, or keep it up on a small always-on box.

Only the configured chat is served — the bot token is a bearer credential, and
anyone holding it could otherwise edit your watchlist.

| Command | Does |
|---|---|
| `/list` | Watchlist with 14-day signal counts; tap a ticker for its history |
| `/add NVDA` · `/remove NVDA` | Edit the watchlist |
| `/history NVDA [days]` | Signals recorded for that name, newest first |
| `/grade NVDA` | CAN SLIM scorecard, PDF attached — re-graded fresh |
| `/levels` · `/on X` · `/off X` | Which detection levels run |
| `/config [X]` · `/set X SETTING VALUE` | Read and change thresholds |
| `/set TSLA X SETTING VALUE` | Change one setting for one ticker |
| `/explain X` | Every setting, its allowed range, and the reasoning |
| `/narrator [SETTING VALUE]` | Who writes the CAN SLIM letters, and what that costs |
| `/grade NVDA cached` | Reuse today's grade instead of re-running it |
| `/reset [X]` · `/changes` | Back to committed defaults; see the live diff |
| `/status` | Market state, data-source health, last run |

Edits land in `state/runtime.json`, an overlay merged over `config.yaml` at load
time. So `config.yaml` stays the reviewable baseline, a bad live change is one
`/reset` away, and the edit survives to the next cron tick without a commit.
Bot edits go through the same bounded validation as the YAML — `/set
volume_anomaly rvol_threshold 500` is refused with the allowed range, not
silently clamped. `2.5M`, `500k` and `1,250,000` all work.

## Commands

```
monitor console             the bot's commands locally, no Telegram needed
monitor bot                 run the Telegram bot (long-polling)
monitor grade TICKER        CAN SLIM scorecard + PDF for one ticker
  --narrate / --no-narrate  override canslim.narrator for this one grade
  --fresh                   ignore today's cached grade and re-run it
monitor run                 one polling cycle (what the cron calls)
  --dry-run                 print alerts instead of sending; leaves state untouched
  --force                   run session-bound detectors even when the market is closed
  --no-grade                don't attach scorecards, whatever attach_canslim says
  --as-of TIMESTAMP         run the clock at an ISO-8601 moment (implies --dry-run)
monitor capture             save bars to a file for replay
  -o PATH                   where to write it (default state/snapshot.json)
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

## What IBKR can and cannot do

**Can — aggregate volume, well.** Bars down to 30 seconds (finer than FMP's
1-minute floor), a real 90-day average dollar volume, 52-week statistics, and
today's option volume for the underlying against its average. That last one is a
genuine unusual-activity read you get *without* an options-flow subscription,
which is why `option_volume` exists as a detector.

**Cannot — individual large prints, through this API.** Tick-by-tick trade data
does exist in IBKR's native TWS API (`reqTickByTickData` with "AllLast"), but
that is a socket protocol against a running TWS instance, not REST. So L2 print
detection still comes from Unusual Whales. If you wanted IBKR to do L2, you'd be
building the always-on process this project was designed to avoid.

**`option_volume` is genuinely coarser than `options_flow`.** It says "the whole
chain is 3× busier than normal, skewed to calls". It does not say who bought
what strike for how much. Read it as confirmation, and check for an earnings date
before reading anything into it — elevated chain volume into earnings is routine.
The figures are day-cumulative, so the ratio is pace-adjusted for how much of the
session has elapsed and suppressed entirely before `min_session_pct`.

## CAN SLIM scorecards

Attached to alerts at or above `run.canslim_min_severity`, and available on
demand with `/grade NVDA`.

**Be clear on what this is.** `can-slim-grader` is an *agent* skill: its intended
operator is an LLM that reads the methodology, gathers data, and exercises
judgement per letter. A five-minute cron has no LLM in the loop, so this
implements the skill's **published pass/partial/fail rubric deterministically** —
the thresholds from its `data-and-scoring-guide.md`, applied in code.

- **What that buys:** free, fast, reproducible, runnable on every alert.
- **What it costs:** the letters that genuinely need judgement — chiefly **N**'s
  "new product/management/condition" story and the quality half of **I** —
  cannot be assessed from numbers. Those are scored on their measurable proxies
  and the report *says so per letter*, rather than implying a judgement it never
  made. A letter with no data reads `unknown`, never `fail`.

Three parts of the skill are used **verbatim**: `relative_strength.py` for the
technicals, `evaluation_template.html` for the report, `html_to_pdf.py` for the
export. Only the letter-scoring loop is local.

Grades are computed **once per ticker per day** and reused — a CAN SLIM verdict
turns on quarterly earnings and a multi-month base, so ten alerts on one busy
name cost one grade. If no PDF engine is present the HTML report is attached
instead; a missing report never costs you the alert.

### The narrator (optional)

`canslim.narrator: llm` puts an LLM back in the loop for the half the numbers
cannot settle. Claude gets the computed scorecard, the skill's methodology, and
optionally a web search, and returns the per-letter commentary plus a judgement
on **N**'s new driver and **I**'s sponsorship quality.

**What it is not allowed to do matters more than what it does.** A model handed a
scorecard will rewrite the arithmetic, so the merge is deliberately asymmetric:

| | Narrator may set the score? |
|---|---|
| **N**, **I** — the judgement letters | yes, that is the point |
| any letter the computed pass left `unknown` | yes — it may have the data we lacked |
| **C, A, S, L, M** with a computed score | **no.** The proposal is discarded, and the rejection is printed on the report |

So the report always tells you which pass produced the letters, and every score
the narrator set — or tried to set and was refused — appears in the Notes
section of the PDF, not just in a log. The verdict and the entry/stop band are
re-derived after any accepted change, so an upgraded letter can't sit next to a
stale verdict.

It is an upgrade to the grade, never a prerequisite for having one. No key, rate
limit, safety refusal, malformed JSON, missing `anthropic` package — each is
recorded as a skip line on the report and you still get the full deterministic
scorecard.

Costs roughly **$0.10-0.40 and 30-90 seconds per ticker per day** on
`claude-opus-5`. The methodology document is identical on every call and sits
behind a prompt-cache breakpoint, so only the first ticker of the hour pays to
read it; combined with the daily grade cache, a busy watchlist is charged once
per name per day. `narrator_effort`, `narrator_research` (the web-search phase),
`narrator_max_tokens` and `narrator_model` are all tunable from `config.yaml` or
`/narrator`.

To see it on one name without touching the config: `monitor grade NVDA --narrate
--fresh`.

## Data source health

A monitor that silently stops seeing data is worse than no monitor, because
silence reads as "nothing is happening". Three distinct failures, three checks:

| Failure | How it's caught | When you hear about it |
|---|---|---|
| **Unresponsive** | the call raises or times out | after `unresponsive_after` consecutive misses (default 3), then once — not every 5 minutes |
| **Stale** | bar timestamps compared against the session clock | when the newest bar falls more than `max_stale_intervals` behind during a session |
| **Corrupt** | negative volume, non-positive prices, high&nbsp;<&nbsp;low, OHLC out of range, out-of-order or duplicate timestamps, implausible intraday jumps | immediately |

A fourth failure sits outside that table because it isn't about a provider at
all: **the cron itself stopping.** Everything `/list`, `/history` and `/status`
show is a record of what the cron saw, so if it died an hour ago an empty
watchlist reads as "nothing is happening" when it means "nobody is looking". The
bot refuses to present those records as current — it says how long ago the last
poll was, and says outright that alerts are being missed if the market is open.

Stale is the dangerous one: the call *succeeds*, so a frozen feed looks perfectly
healthy from outside. Corrupt is escalated immediately rather than after N
occurrences, because a corrupt bar produces a confident, completely false alert —
so corrupt bars are **dropped before any detector sees them**. An unadjusted
10-for-1 split is caught this way instead of being reported as a -90% move.
Recovery is announced too, so you know when to trust a feed again. `/status`
shows current health; the run footer reports changes.

### Nothing is served from cache without saying so

A monitor that answers with old data is worse than one that admits it can't
answer, so the rule is: fetch fresh, and where something is reused, stamp it.

- **Every provider request forbids caching** (`Cache-Control: no-cache, no-store`,
  `Pragma: no-cache`). `requests` keeps no cache of its own, but proxies and CDNs
  in the path do, and any of them will happily serve a minute-old quote.
- **A frozen event feed is caught by the watchlist going quiet, not one ticker.**
  A thin name really can have no dark-pool prints for an hour, so per-ticker
  silence proves nothing. If *no* ticker's newest print has advanced since the
  last run and the newest one on file is older than
  `run.max_feed_silence_minutes`, the feed is reported as frozen.
- **Bar staleness is checked during extended hours too**, on a wider budget
  (pre-market bars are legitimately sparse). It used to be skipped outside the
  regular session, which meant a feed that froze at 07:00 was never caught.
- **Scorecards expire.** A CAN SLIM verdict barely moves intraday, but the price
  and pivot printed beside it do, so a cached grade older than
  `run.canslim_max_age_minutes` (default 90) is re-graded rather than reused, and
  a reused one says how old its figures are.
- **`/grade` re-fetches by default.** Someone who types it is asking about now.
  The cached grade is the thing you have to ask for — `/grade NVDA cached` — not
  the other way round.
- **A failed grade returns nothing, never stale figures.** Same for a failed
  fetch: the detector stays silent and the run footer says why.

### Replay, for tuning thresholds

The one place old data *is* the point. `rvol_threshold: 2.0` is a guess until you
have watched it against a real session, and you cannot iterate on a guess at one
cron tick every five minutes.

```bash
monitor capture -o state/snapshot.json          # from whatever provider is live
# then, in a config with providers.bars: snapshot and snapshot.path set:
monitor run -c config.replay.yaml --as-of 2026-07-22T11:31:00-04:00
```

Change a threshold, re-run, see what would have fired. Same engine, same
detectors, same code path the cron uses — no API budget, no key. It is also how
the pipeline can be exercised in CI, where there are no credentials at all.

Because replay hands the engine stale data deliberately, it is fenced:

- **No lookahead.** `--as-of` truncates the file at the run clock, and the footer
  says how many later bars were withheld. A replay that can see its own future
  makes every threshold you tune look better than it is.
- **`--as-of` forces `--dry-run`.** An alert stamped last Tuesday arriving on your
  phone today is worse than no alert.
- **The run footer leads with `⏪ Replay run`** and names the file, its source and
  the age of the newest visible bar. Leaving `bars: snapshot` in a committed
  config and believing you are being alerted on a live market is the failure this
  guards against.
- **The interval must match.** A 30-minute bar judged against a 5-minute baseline
  is a fabricated anomaly, so a mismatch is refused rather than averaged.

A capture can carry the L2/L3 feeds too — `prints`, `option_trades` and
`option_volume` alongside `bars` — so `providers.trades`, `flow` and
`option_volume` can each be pointed at the file independently. A detector whose
feed is *absent* from the snapshot raises rather than reading empty: silence
would look like a quiet market instead of a missing feed.

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
- **Interactive commands need `monitor bot` running.** The cron alerts on its
  own; the bot only answers while its process is up. That's the cost of long
  polling instead of a public webhook endpoint.
- **IBKR needs a logged-in gateway** and cannot run on CI, as above.
- **CAN SLIM letters are scored programmatically** by default, not by the skill's
  own agent judgement. Turning [the narrator](#the-narrator-optional) on restores
  the judgement half at a per-grade cost — and even then it can only score N, I
  and letters that were `unknown`; the measurable letters stay arithmetic.
- **Not investment advice, and not a trading signal.** Unusual size is a
  prompt to go look, nothing more.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 380 tests, no network needed
```

No network anywhere in the suite. Coverage spans provider payload parsing,
detection logic, config bounds, state transitions, health escalation, the bot
command surface, CAN SLIM scoring, and the full engine pipeline with stub
providers.

Two deliberate choices in the test design:

- **Ten ticker profiles**, not one synthetic mega-cap (`tests/tickers.py`) —
  mega-cap, high-beta, mid, small, a $3.70 stock, a $742,000 stock, and an
  illiquid micro-cap. Logic tuned on one price scale misbehaves on another: the
  low-priced and ultra-high-priced profiles exist specifically to pin the cases
  where share-count and dollar thresholds disagree.
- **Fixtures are seeded with `zlib.crc32`, not `hash()`** — Python randomises
  string hashing per process, so seeding from `hash()` produces tests that pass
  locally and fail in CI on an unlucky seed. Ask how I know.

CAN SLIM tests skip cleanly when the skill isn't checked out, so CI doesn't fail
on an absent optional sibling repo.
