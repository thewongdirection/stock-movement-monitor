"""Shared HTTP plumbing: rate limiting, retries, and typed failures."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Mapping, Sequence

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class ProviderError(RuntimeError):
    """A provider call failed in a way the caller should surface, not swallow."""

    def __init__(self, message: str, *, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class SetupError(ProviderError):
    """A provider cannot be constructed at all — a missing key or bad setting.

    Distinct from a call failure because it is not per-ticker: reporting it once
    per run is right, reporting it once per symbol is noise.
    """


class RateLimitedSession:
    """A requests session with a floor on inter-request spacing and retries.

    SEC asks for no more than 10 requests/second and a descriptive User-Agent;
    the commercial APIs have their own per-minute caps. One knob covers both.
    """

    def __init__(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        min_interval: float = 0.0,
        timeout: int = 30,
        max_attempts: int = 4,
    ):
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._last_call = 0.0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        response = self.get(url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:200].replace("\n", " ")
            raise ProviderError(
                f"expected JSON but got {response.headers.get('content-type', '?')}: "
                f"{snippet}",
                status=response.status_code,
                url=url,
            ) from exc

    def get(
        self, url: str, params: Mapping[str, Any] | None = None
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                self._backoff(attempt, f"{type(exc).__name__}: {exc}", url)
                continue

            if response.status_code in RETRY_STATUS and attempt < self.max_attempts:
                self._backoff(attempt, f"HTTP {response.status_code}", url, response)
                continue

            if response.status_code >= 400:
                raise ProviderError(
                    _explain(response),
                    status=response.status_code,
                    url=url,
                )
            return response

        raise ProviderError(
            f"giving up after {self.max_attempts} attempts: {last_error}", url=url
        )

    def _backoff(
        self,
        attempt: int,
        reason: str,
        url: str,
        response: requests.Response | None = None,
    ) -> None:
        if response is not None and response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), 30.0)
                log.warning("%s on %s; honouring Retry-After %ss", reason, url, delay)
                time.sleep(delay)
                return
        delay = min(2.0 ** (attempt - 1), 8.0) + random.uniform(0, 0.4)
        log.warning("%s on %s; retrying in %.1fs (attempt %d)", reason, url, delay, attempt)
        time.sleep(delay)

    def close(self) -> None:
        self.session.close()


def _explain(response: requests.Response) -> str:
    """Turn a failure into something actionable rather than a bare status code."""
    status = response.status_code
    body = response.text[:300].replace("\n", " ").strip()
    hints = {
        401: "authentication rejected — check the API key secret is set and current",
        403: "forbidden — the key may lack entitlement for this endpoint, or your "
        "plan does not include it",
        404: "not found — the endpoint path is probably wrong; correct it under "
        "providers in config.yaml and re-run `monitor verify`",
        429: "rate limited",
    }
    hint = hints.get(status, "request failed")
    return f"HTTP {status}: {hint}" + (f" | body: {body}" if body else "")


def first_present(payload: Any, keys: Sequence[str], default: Any = None) -> Any:
    """Fetch the first key present in a dict — APIs rename fields between versions."""
    if not isinstance(payload, Mapping):
        return default
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


def unwrap_list(payload: Any, keys: Sequence[str] = ("data", "results", "chains")) -> list:
    """Return the list from a response that may or may not be enveloped."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []
