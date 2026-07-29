# Setup, step by step

From nothing to alerts landing on your phone. Follow it in order — each step is
checkable, so you find out something is wrong at the step that broke it rather
than three steps later.

Two things worth knowing before you start:

- **You can try the whole thing before signing up for anything.** Steps 1–3 need
  no keys and no Telegram bot. Skip ahead if you just want to look around.
- **Every paid key is optional.** With none of them you still get SEC Form 4
  insider alerts, which are free. Each key you add turns on more detectors.

Time: about 10 minutes to try locally, about 30 to have it running on a cron.

---

## Step 1 — Get the code running

Needs Python 3.11 or newer.

```bash
git clone https://github.com/thewongdirection/stock-movement-monitor
cd stock-movement-monitor

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

**Check it:**

```bash
PYTHONPATH=src python -m monitor --help
```

You should see the command list. If Python complains about the version, check
`python3 --version` — 3.10 and earlier will not work.

> From here on every command starts with `PYTHONPATH=src python -m monitor`.
> To shorten that to just `monitor`, run `pip install -e .` once.

---

## Step 2 — Create your config

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and put your tickers at the top. Leave everything else alone
for now — every threshold has a documented default and you can tune them later
from the bot.

```yaml
tickers:
  - NVDA
  - AAPL
  - TSLA
```

**Check it:**

```bash
PYTHONPATH=src python -m monitor validate
```

This prints your tickers, which detectors are on, and — importantly — which
environment variables are still missing. Expect it to list several right now;
that is what the next steps fix. It exits non-zero on a real config error, so it
is also what you would run in CI.

---

## Step 3 — Try the bot's whole command surface, with no bot

```bash
PYTHONPATH=src python -m monitor console
```

This is the Telegram bot's entire command set, in your terminal, with no token
and no network. Type `/help` for the list. Things worth trying:

```
/list                      your watchlist
/levels                    the detection levels and what each needs
/status                    market state, data-source health, missing keys
/config volume_anomaly     current thresholds with allowed ranges
/set volume_anomaly rvol_threshold 3
/changes                   what you just changed vs the committed config
/reset                     put it back
```

Buttons are numbered — type `1` to follow one.

`/status` will tell you plainly that several data sources cannot be reached and
that **silence from those detectors is not an all-clear.** That is the honest
state of a monitor with no keys, and it is the point of the next step.

Config changes you make here are real: they persist to `state/runtime.json` and
the cron picks them up. `/reset` undoes them.

---

## Step 4 — Add API keys for the detectors you want

Each key is optional and turns on specific detectors. Skip any you don't want.

| Key | Cost | Turns on | Where |
|---|---|---|---|
| `SEC_USER_AGENT` | free | `insider_trades` — Form 4 buys and sells | just your own email |
| `FMP_API_KEY` | **paid — Starter or above** | `volume_anomaly` (L1) and CAN SLIM scorecards | [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs) |
| `UW_API_KEY` | paid | `dark_pool`, `block_trades` (L2), `options_flow` (L3) | [unusualwhales.com](https://unusualwhales.com/settings/api-dashboard) |
| `ANTHROPIC_API_KEY` | pay per use | Claude writing the CAN SLIM judgement letters | [console.anthropic.com](https://console.anthropic.com/) |

`SEC_USER_AGENT` is not really a key — SEC's fair-access policy asks for a
contact address that reaches you. Use the real thing; a fake one gets you
blocked, and the monitor refuses to send the example value.

**On the FMP tier:** the price-history endpoints this project needs — intraday
bars for L1, daily bars for the CAN SLIM technicals — sit behind FMP's Starter
plan or above. A free key authenticates fine and then 403s on those endpoints,
which `monitor verify` will show you as *"the key may lack entitlement for this
endpoint, or your plan does not include it"*. So the genuinely free
configuration is **insider alerts only**: keep `insider_trades` on and set
`enabled: false` on everything else.

```bash
export SEC_USER_AGENT="stock-movement-monitor you@example.com"
export FMP_API_KEY="..."
export UW_API_KEY="..."
```

To keep them across shells, put them in a `.env` file (already gitignored) and
`source .env`.

**Check it:**

```bash
PYTHONPATH=src python -m monitor verify
```

This actually calls every endpoint and reports what answered. It is the step
that catches a wrong key, a plan that doesn't include an endpoint, or a changed
URL — and it tells you which config value to correct rather than making you read
code.

---

## Step 5 — Do a dry run

```bash
PYTHONPATH=src python -m monitor run --dry-run --force
```

`--dry-run` prints alerts instead of sending them and leaves your state file
untouched. `--force` runs the session-bound detectors even when the market is
shut, so you can test outside trading hours.

Read the output. You want to see either alerts, or an explanation of why there
were none. Any provider that failed is listed under **Errors this run** with a
message saying what to do about it.

---

## Step 6 — Set up the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) and send
   `/newbot`. Follow the prompts; it gives you a token that looks like
   `123456789:AAH...`.
2. Export it, then **send your new bot any message** — it cannot find you until
   you speak first:

   ```bash
   export TELEGRAM_BOT_TOKEN="123456789:AAH..."
   PYTHONPATH=src python -m monitor telegram-chat-id
   ```

3. That prints your chat id. Export it:

   ```bash
   export TELEGRAM_CHAT_ID="987654321"
   ```

**Check it:**

```bash
PYTHONPATH=src python -m monitor test-alert
```

A sample alert should arrive in Telegram. If it doesn't, the error message will
say whether the token or the chat id was the problem.

Only that one chat is served. The token is a bearer credential — anyone holding
it could otherwise edit your watchlist.

---

## Step 7 — Optional: CAN SLIM scorecards on your alerts

```bash
git clone --depth 1 https://github.com/thewongdirection/can-slim-grader vendor/can-slim-grader
```

That's it — grading turns itself on once the skill is on disk and `FMP_API_KEY`
is set. Try it:

```bash
PYTHONPATH=src python -m monitor grade NVDA
```

You get a scorecard and a PDF. Read [what this actually
grades](README.md#can-slim-scorecards) before relying on it: the letters are
scored programmatically against the skill's published rubric, which is not the
same as the skill's own agent judgement.

**Optionally**, let Claude supply the judgement half — the per-letter commentary
plus **N**'s "new" driver and **I**'s sponsorship quality:

```bash
pip install -r requirements-narrator.txt
export ANTHROPIC_API_KEY="sk-ant-..."
PYTHONPATH=src python -m monitor grade NVDA --narrate --fresh
```

If you like it, turn it on for good with `/narrator llm` in the bot or
`narrator: llm` under `canslim:` in `config.yaml`. It costs roughly $0.10–0.40
per ticker per day and **cannot change a computed letter** — see [the
narrator](README.md#the-narrator-optional).

---

## Step 8 — Put it on the cron

The repo ships `.github/workflows/monitor.yml`, which polls every 5 minutes on
GitHub Actions. No server to keep alive.

1. Push your `config.yaml` to your own copy of the repo.
2. Add your keys under **Settings → Secrets and variables → Actions → New
   repository secret**. Use the same names as the environment variables:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `FMP_API_KEY`, `UW_API_KEY`,
   `SEC_USER_AGENT`, and `ANTHROPIC_API_KEY` if you want the narrator.
3. Go to **Actions**, pick the **monitor** workflow, and hit **Run workflow** —
   tick `dry_run` for the first one.
4. Read the run log. It ends with the same summary the dry run printed.

Once a dry run looks right, untick `dry_run` and let the schedule take over.

**Three things to know about the cron:**

- **Add the secrets before enabling the schedule.** Without
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` the run has nowhere to send
  alerts, so it exits non-zero and GitHub emails you a failure every time it
  fires. That is deliberate — a monitor that cannot deliver is broken, and
  should not look healthy — but it means an unconfigured repo will mail you
  hourly until you either add the secrets or disable the workflow.
- **GitHub disables scheduled workflows after 60 days of repo inactivity.** A
  commit or a manual run resets the clock.
- **`*/5` is a request, not a promise.** GitHub's scheduler is best-effort and
  drops runs under load — on a quiet repo, a five-minute cron in practice fires
  closer to hourly, and irregularly. Fine against a 2-hour target; if you need
  reliable five-minute latency, run the cron somewhere you control.

---

## Step 8b — Optional: tune thresholds against a real session

The defaults are conventional, not tailored to your names. Before trusting them,
capture a session and replay it:

```bash
PYTHONPATH=src python -m monitor capture -o state/snapshot.json
```

That prints how many bars it got per ticker and the config block to paste. Make a
`config.replay.yaml` from it:

```yaml
tickers: [NVDA]
detectors:
  volume_anomaly: {enabled: true, bar_interval: 30min}
  block_trades: {enabled: false}
  dark_pool: {enabled: false}
  options_flow: {enabled: false}
  option_volume: {enabled: false}
  insider_trades: {enabled: false}
run:
  attach_canslim: false
  cold_start_lookback_minutes: 400   # wide open, so every bar is a candidate
providers:
  bars: snapshot
  snapshot: {path: state/snapshot.json}
```

Then replay any moment in it:

```bash
PYTHONPATH=src python -m monitor run -c config.replay.yaml \
  --as-of 2026-07-22T11:31:00-04:00
```

Change `rvol_threshold`, run it again, and see what would have fired. Free, fast,
repeatable. Two things to know: `--as-of` forces `--dry-run`, and it truncates the
file at that clock so the replay cannot see its own future. `bar_interval` must
match the interval you captured at.

## Step 8c — Optional: unusual option activity via IBKR

The one unusual-activity signal that needs no Unusual Whales subscription. It
reads the **whole option chain against its own average**, pace-adjusted for how
much of the session has run. No strike, no premium, no direction — but a genuine
signal, and free with an IBKR account.

**It cannot run on GitHub Actions.** IBKR has no API key; it talks to a Client
Portal Gateway that must be running and *interactively logged in*, with a
session that expires roughly daily. An ephemeral runner has nothing to log into.
So this needs a machine you control that stays on.

1. **Get the gateway.** Download the Client Portal Gateway from IBKR, or run
   their container image. It listens on `https://localhost:5000` with a
   self-signed certificate, which this project expects.

2. **Log in.** Open `https://localhost:5000` in a browser, accept the
   certificate warning, and sign in with your IBKR credentials. You will need to
   repeat this roughly daily — that is the operational cost of IBKR. Tools like
   IBC can automate the re-login if you want it unattended.

3. **Use the IBKR config**, which is separate so the Actions cron stays green:

   ```bash
   PYTHONPATH=src python -m monitor verify -c config.ibkr.yaml
   ```

   You want to see, for each ticker:

   ```
   ── IBKR (option volume) ──
     ok — 2,452,910 contracts today vs 3,576,620 average
        0.69x — below the threshold (min_ratio 2.5)
   ```

   A ratio below the threshold is the normal state. Most days are quiet.

4. **Point a local cron at it.** On that machine:

   ```
   */5 * * * *  cd /path/to/stock-movement-monitor && PYTHONPATH=src \
     .venv/bin/python -m monitor run -c config.ibkr.yaml >> state/cron.log 2>&1
   ```

`config.ibkr.yaml` also switches **bars** to IBKR — 30-second granularity, a real
90-day dollar ADV, and no FMP plan tier to worry about. Change `bars` back to
`fmp` if you would rather keep the gateway load down.

## Step 9 — Run the bot when you want to talk to it

The cron sends alerts on its own. Interactive commands only work while the bot
process is running:

```bash
PYTHONPATH=src python -m monitor bot
```

Start it when you want to change something, or keep it up on a small always-on
box. Everything you tried in the console in step 3 works here, plus `/grade` and
`/history`.

---

## Where things live

| Path | What |
|---|---|
| `config.yaml` | your reviewable baseline — commit it |
| `state/runtime.json` | live edits made from the bot; `/reset` clears it |
| `state/monitor.db` | dedup keys, watermarks, signal history |
| `state/reports/` | generated CAN SLIM HTML and PDFs |
| `state/snapshot.json` | captured bars for replay, if you made one |
| `.env` | your keys, gitignored — never commit it |

On GitHub Actions the whole `state/` directory lives in the Actions cache.
Losing it is safe: the monitor falls back to `run.cold_start_lookback_minutes`
rather than replaying a whole day of alerts.

---

## Troubleshooting

**`monitor validate` says environment variables are not set.** That is a warning,
not an error, and it lists exactly which detectors are affected. Detectors
without their key stay silent.

**`monitor verify` reports a 404 on an Unusual Whales path.** Their API docs are
not readable without an account, so the paths this ships with are best-effort.
Correct the one that failed under `providers.unusual_whales.paths` in
`config.yaml` — no code change needed.

**No alerts at all during market hours.** Run `/status` in the bot. It will tell
you when the last poll was, which sources are unreachable, and whether any feed
looks frozen. If the last poll is hours old, the cron is the problem, not the
market.

**Alerts stopped and everything looks healthy.** That is the case this project
worries about most, so there are specific checks for it: a feed that answers
normally but never advances is reported as frozen after
`run.max_feed_silence_minutes`, and the bot refuses to present records from a
dead cron as current.

**`/grade` says PDF unavailable.** The HTML report is attached instead. Install a
PDF engine if you want PDFs — the skill's `html_to_pdf.py` says which.

**The narrator is on but nothing is narrated.** The reason is printed on the
report itself as a skip line, and `monitor grade TICKER --narrate --fresh` shows
it directly. Most often: `ANTHROPIC_API_KEY` unset, or `anthropic` not installed.
