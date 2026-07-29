"""SEC EDGAR — Form 4 insider transactions, read from the primary source.

This is the one signal in the project where direction is genuinely known rather
than inferred. Nobody has to guess whether an officer was buying: they filed a
document saying so, under penalty of perjury.

The cost is latency. Form 4 is due within two business days of the trade, so
every alert here is news about something that already happened. That is worth
knowing and worth saying in the alert, which is why `filing_lag_days` exists.

EDGAR is free and has no key, but it does have rules: a `User-Agent` carrying
real contact details is mandatory (requests without one are blocked outright),
and the request rate is capped. Both are honoured here.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from ..clock import ET as EASTERN
from ..models import InsiderTrade
from .base import BadResponse, HttpClient, NotConfigured, Unreachable

log = logging.getLogger("monitor.sources.sec")

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

#: EDGAR asks for no more than 10 requests a second. One filing needs one
#: request, and a busy week for a large issuer is a few dozen filings.
REQUEST_GAP_SECONDS = 0.12

#: Transaction codes worth reading. The rest (grants, gifts, tax withholding)
#: are compensation mechanics, not decisions about the stock.
MEANINGFUL_CODES = frozenset({"P", "S"})


class SecSource:
    name = "sec"

    def __init__(self, http: HttpClient, user_agent: str | None, *,
                 max_filings: int = 40, sleeper=time.sleep):
        if not user_agent or "@" not in user_agent:
            raise NotConfigured(
                self.name,
                "SEC_USER_AGENT must be a contact address, e.g. "
                "'stock-monitor you@example.com' — EDGAR blocks anonymous requests",
            )
        self.http = http
        self.http.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self.max_filings = max_filings
        self._sleep = sleeper
        self._cik_map: dict[str, int] | None = None

    # -- ticker -> CIK ------------------------------------------------------
    def cik(self, ticker: str) -> int:
        if self._cik_map is None:
            payload = self.http.get_json(TICKER_MAP_URL, ticker=ticker)
            if not isinstance(payload, dict):
                raise BadResponse(self.name, "company_tickers.json was not an object", ticker)
            self._cik_map = {
                str(row["ticker"]).upper(): int(row["cik_str"])
                for row in payload.values()
                if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None
            }
            log.debug("loaded %d EDGAR ticker mappings", len(self._cik_map))

        found = self._cik_map.get(ticker.upper())
        if found is None:
            raise Unreachable(
                self.name, f"{ticker.upper()} is not in EDGAR's ticker index", ticker
            )
        return found

    # -- filings ------------------------------------------------------------
    def filings(self, ticker: str, since: date) -> list[InsiderTrade]:
        ticker = ticker.upper()
        cik = self.cik(ticker)
        payload = self.http.get_json(SUBMISSIONS_URL.format(cik=cik), ticker=ticker)
        recent = (payload or {}).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        if not forms:
            # An issuer with no recent filings at all is possible but unusual;
            # far more often this means the payload shape changed.
            raise BadResponse(self.name, "submissions JSON had no recent filings block", ticker)

        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        documents = recent.get("primaryDocument", [])

        trades: list[InsiderTrade] = []
        seen = 0
        for form, accession, filed, document in zip(forms, accessions, dates, documents):
            if form != "4" or seen >= self.max_filings:
                continue
            try:
                filed_on = datetime.strptime(filed, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if filed_on < since:
                # `recent` is newest-first, so the first old filing ends the scan.
                break
            seen += 1
            try:
                trades.extend(self._read_filing(ticker, cik, accession, document, filed_on))
            except (BadResponse, Unreachable) as exc:
                log.warning("skipping Form 4 %s for %s: %s", accession, ticker, exc)
            self._sleep(REQUEST_GAP_SECONDS)

        return trades

    def _read_filing(self, ticker: str, cik: int, accession: str,
                     document: str, filed_on: date) -> list[InsiderTrade]:
        bare = accession.replace("-", "")
        # `primaryDocument` points at the human-readable rendering
        # (`xslF345X05/...`); the machine-readable XML sits beside it.
        raw_doc = document.split("/")[-1]
        url = ARCHIVE_URL.format(cik=cik, accession=bare, document=raw_doc)

        response = self.http.session.get(url, timeout=self.http.timeout)
        if not response.ok:
            raise Unreachable(self.name, f"HTTP {response.status_code} for {url}", ticker)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise BadResponse(self.name, f"{raw_doc} is not valid XML: {exc}", ticker) from exc

        return parse_form4(
            root, ticker=ticker, accession=accession, filed_on=filed_on,
            url=ARCHIVE_URL.format(cik=cik, accession=bare, document=document),
        )


# --------------------------------------------------------------------------- #
# Form 4 parsing
# --------------------------------------------------------------------------- #

def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    if found is None:
        return default
    # Most Form 4 fields wrap their content in a <value> child; some do not.
    value = found.find("value")
    target = value if value is not None else found
    return (target.text or "").strip() or default


def _float(node: ET.Element | None, path: str) -> float | None:
    raw = _text(node, path)
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _flag(node: ET.Element | None, path: str) -> bool:
    return _text(node, path).strip().lower() in ("1", "true")


def parse_form4(root: ET.Element, *, ticker: str, accession: str,
                filed_on: date, url: str | None = None) -> list[InsiderTrade]:
    """Turn one Form 4 document into zero or more transaction lines.

    Only open-market purchases and sales (codes P and S) are returned. Grants,
    option exercises and tax withholding are the mechanics of being paid in
    stock, not a view on the stock, and including them turns every vesting date
    into a false alarm.
    """
    owner = root.find("reportingOwner")
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None

    insider = _text(owner, "reportingOwnerId/rptOwnerName", "unknown")
    title = _text(relationship, "officerTitle") or _describe_role(relationship)
    is_officer = _flag(relationship, "isOfficer")
    is_director = _flag(relationship, "isDirector")
    is_ten_pct = _flag(relationship, "isTenPercentOwner")

    document_text = "".join(root.itertext()).lower()
    planned = "10b5-1" in document_text or _flag(root, "aff10b5One")

    filed_at = datetime.combine(filed_on, datetime.min.time()).replace(tzinfo=EASTERN)

    trades: list[InsiderTrade] = []
    table = root.find("nonDerivativeTable")
    for txn in (table.findall("nonDerivativeTransaction") if table is not None else []):
        code = _text(txn, "transactionCoding/transactionCode").upper()
        if code not in MEANINGFUL_CODES:
            continue
        shares = _float(txn, "transactionAmounts/transactionShares")
        price = _float(txn, "transactionAmounts/transactionPricePerShare")
        traded_on = _text(txn, "transactionDate")
        if shares is None or not traded_on:
            continue
        if price is None or price <= 0:
            # A P or S with no price is almost always a reporting artefact, and
            # a zero-dollar "purchase" would sail through every value filter.
            continue
        if traded_on[:10] > filed_on.isoformat():
            # A trade cannot be reported before it happens. EDGAR does carry
            # the occasional mistyped transaction date, and letting one through
            # produces a negative filing lag and an alert that reads as nonsense.
            log.warning("Form 4 %s: transaction dated %s but filed %s — skipping",
                        accession, traded_on[:10], filed_on)
            continue

        trades.append(InsiderTrade(
            ticker=ticker,
            insider=insider,
            title=title,
            code=code,
            shares=shares,
            price=price,
            traded_on=traded_on[:10],
            filed_at=filed_at,
            accession=accession,
            is_officer=is_officer,
            is_director=is_director,
            is_ten_percent=is_ten_pct,
            planned_10b5_1=planned,
            shares_after=_float(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
            url=url,
        ))
    return trades


def _describe_role(relationship: ET.Element | None) -> str:
    if relationship is None:
        return "insider"
    roles = [
        label for label, path in (
            ("Director", "isDirector"),
            ("Officer", "isOfficer"),
            ("10% owner", "isTenPercentOwner"),
        ) if _flag(relationship, path)
    ]
    return ", ".join(roles) or "insider"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
