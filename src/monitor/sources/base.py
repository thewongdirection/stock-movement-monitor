"""Shared plumbing for every data source: typed failures, retries, redaction.

Two things this module refuses to do.

**It never lets a credential reach a log line.** Every source here authenticates
by query string or header, and the natural thing to write in a retry warning is
the URL that failed — which for FMP is `?apikey=<the actual key>`. `redact()`
runs over every message this package emits, so a log file can be pasted into a
bug report without laundering it first.

**It never turns a failure into an empty list.** A source that cannot be reached
returns *no information*, which is not the same as "nothing happened". Every
failure mode below raises, and the engine converts it into a visible SourceIssue.
Swallowing these would make a dead feed indistinguishable from a calm market —
the single most dangerous failure this project can have.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Any, Protocol

import requests

from ..models import Bar, InsiderTrade, OptionChain, SourceIssue, Trade

log = logging.getLogger("monitor.sources")

#: Query parameters whose values must never be printed.
SECRET_PARAMS = ("apikey", "api_key", "token", "access_token", "key", "secret", "password")

_SECRET_QUERY = re.compile(
    r"(?i)\b(" + "|".join(SECRET_PARAMS) + r")=([^&\s\"']+)"
)
_TELEGRAM_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_\-]+")
_BEARER = re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._\-=+/]+")


def redact(text: Any) -> str:
    """Strip anything that looks like a credential out of a string."""
    out = str(text)
    out = _SECRET_QUERY.sub(lambda m: f"{m.group(1)}=***", out)
    out = _TELEGRAM_PATH.sub("/bot***", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} ***", out)
    return out


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #

class SourceError(Exception):
    """A source could not supply trustworthy data.

    `kind` is the vocabulary the operator sees, so it stays short and concrete:
    unreachable, corrupt, empty, stale, unconfigured.
    """

    kind = "unreachable"

    def __init__(self, source: str, detail: str, ticker: str = "*"):
        self.source = source
        self.ticker = ticker
        self.detail = redact(detail)
        super().__init__(f"{source}/{ticker}: {self.kind}: {self.detail}")

    def issue(self) -> SourceIssue:
        return SourceIssue(source=self.source, ticker=self.ticker,
                           kind=self.kind, detail=self.detail)


class Unreachable(SourceError):
    """DNS, TCP, TLS, timeout, or an HTTP status that means "try later"."""
    kind = "unreachable"


class BadResponse(SourceError):
    """Reached it, but what came back was not the shape it promised."""
    kind = "corrupt"


class EmptyResponse(SourceError):
    """Reached it, understood it, and it had nothing for this ticker."""
    kind = "empty"


class StaleData(SourceError):
    """Data arrived, but it is too old to act on."""
    kind = "stale"


class NotConfigured(SourceError):
    """A credential or endpoint the source needs was not supplied."""
    kind = "unconfigured"


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #

class BarSource(Protocol):
    name: str

    def bars(self, ticker: str, minutes: int, sessions: int) -> list[Bar]:
        """Intraday OHLCV, oldest first, timestamped in US/Eastern."""


class OptionSource(Protocol):
    name: str

    def chain(self, ticker: str, max_days_to_expiry: int) -> OptionChain:
        """Listed contracts with open interest. `previous` is filled by the engine."""


class InsiderSource(Protocol):
    name: str

    def filings(self, ticker: str, since: date) -> list[InsiderTrade]:
        """Form 4 lines filed on or after `since`."""


class TradeSource(Protocol):
    name: str

    def trades(self, ticker: str, since: datetime) -> list[Trade]:
        """Individual prints. Needed for block detection; most feeds do not have it."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpClient:
    """A small retrying HTTP client with redaction wired in.

    Retries only on transport errors and the status codes that actually mean
    "later" — a 401 is retried zero times, because a wrong API key does not
    become right on the third attempt, and hammering an auth endpoint is how
    keys get suspended.
    """

    def __init__(
        self,
        source: str,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.5,
        verify: bool = True,
        headers: dict[str, str] | None = None,
        sleeper=time.sleep,
    ):
        self.source = source
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.verify = verify
        self._sleep = sleeper
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 ticker: str = "*") -> Any:
        last: str = "no attempt made"
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(
                    url, params=params, timeout=self.timeout, verify=self.verify
                )
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in _RETRY_STATUS:
                    last = f"HTTP {response.status_code}"
                elif not response.ok:
                    raise Unreachable(
                        self.source,
                        f"HTTP {response.status_code} for {url} — {response.text[:200]}",
                        ticker,
                    )
                else:
                    try:
                        return response.json()
                    except ValueError:
                        raise BadResponse(
                            self.source,
                            f"expected JSON from {url}, got {response.text[:200]!r}",
                            ticker,
                        ) from None

            if attempt < self.retries:
                delay = self.backoff * (2 ** attempt)
                log.warning(
                    "%s: %s (attempt %d/%d), retrying in %.1fs — %s",
                    self.source, redact(last), attempt + 1, self.retries + 1,
                    delay, redact(url),
                )
                self._sleep(delay)

        raise Unreachable(self.source, f"{last} after {self.retries + 1} attempts: {url}", ticker)

    def close(self) -> None:
        self.session.close()


def require(value: str | None, source: str, what: str) -> str:
    """Fail loudly and early for a missing credential, naming the env var."""
    if not value:
        raise NotConfigured(source, f"{what} is not set")
    return value
