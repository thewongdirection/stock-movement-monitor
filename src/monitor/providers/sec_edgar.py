"""SEC EDGAR — Form 4 insider transactions, straight from the source.

Free, authoritative, no key. The flow is:

1. ``/files/company_tickers.json``      ticker -> CIK
2. ``/submissions/CIK##########.json``  the issuer's recent filings
3. ``/Archives/.../<form4>.xml``        the filing itself, parsed

One thing worth keeping in mind about latency: Form 4 is due within **two
business days of the transaction**, so even a perfect real-time pipeline
reports a trade that already happened. "Fast" here means fast relative to the
filing hitting EDGAR, which this achieves within one cron interval.

SEC's fair-access policy requires a descriptive User-Agent with contact info
and asks for at most 10 requests/second; both are enforced here.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from ..models import InsiderTransaction
from .base import ProviderError, RateLimitedSession, SetupError

log = logging.getLogger(__name__)

WWW = "https://www.sec.gov"
DATA = "https://data.sec.gov"
XSL_PREFIX = re.compile(r"^xsl[^/]*/", re.IGNORECASE)

# Form 4 transaction codes grouped the way an analyst would read them.
CODE_CATEGORY = {
    "P": "purchase",    # open-market or private purchase
    "S": "sale",        # open-market or private sale
    "A": "grant",       # grant, award or other acquisition from the issuer
    "M": "exercise",    # exercise or conversion of a derivative
    "C": "exercise",    # conversion of a derivative security
    "X": "exercise",    # exercise of an in-the-money derivative
    "F": "tax",         # shares withheld to cover exercise price or tax
    "G": "gift",        # bona fide gift
    "D": "other",       # disposition to the issuer
    "V": "other",       # voluntary early report
}

CODE_LABEL = {
    "P": "Open-market purchase",
    "S": "Open-market sale",
    "A": "Grant / award",
    "M": "Option exercise",
    "C": "Conversion",
    "X": "Derivative exercise",
    "F": "Tax withholding",
    "G": "Gift",
    "D": "Disposition to issuer",
}


class SECEdgarProvider:
    name = "sec_edgar"

    def __init__(self, user_agent: str, timeout: int = 30):
        if not user_agent or "@" not in user_agent:
            raise SetupError(
                "SEC requires a descriptive User-Agent containing contact "
                'details, e.g. "stock-movement-monitor you@example.com". Set '
                "SEC_USER_AGENT or providers.sec.user_agent in config.yaml."
            )
        if is_placeholder_user_agent(user_agent):
            raise SetupError(
                f"the SEC User-Agent is still the example value ({user_agent!r}). "
                "SEC's fair-access policy expects a contact address that reaches "
                "you — put your own email in SEC_USER_AGENT or "
                "providers.sec.user_agent."
            )
        self.http = RateLimitedSession(
            headers={
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            min_interval=0.15,  # ~6.7 req/s, inside SEC's 10/s ceiling
            timeout=timeout,
        )
        self._cik_map: dict[str, str] | None = None

    # -- ticker -> CIK ----------------------------------------------------
    def cik_for(self, ticker: str) -> str | None:
        if self._cik_map is None:
            self._cik_map = self._load_cik_map()
        return self._cik_map.get(ticker.upper())

    def _load_cik_map(self) -> dict[str, str]:
        payload = self.http.get_json(f"{WWW}/files/company_tickers.json")
        out: dict[str, str] = {}
        rows = payload.values() if isinstance(payload, dict) else payload
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker", "")).upper()
            cik = row.get("cik_str") or row.get("cik")
            if ticker and cik is not None:
                out[ticker] = str(int(cik)).zfill(10)
        if not out:
            raise ProviderError("SEC ticker->CIK map came back empty")
        log.debug("loaded %d ticker->CIK mappings", len(out))
        return out

    # -- filings ----------------------------------------------------------
    def recent_form4_filings(
        self, ticker: str, since: date
    ) -> list[dict[str, Any]]:
        """Form 4 filings for an issuer, newest first."""
        cik = self.cik_for(ticker)
        if cik is None:
            raise ProviderError(
                f"{ticker} is not in SEC's ticker->CIK map. Check the symbol; "
                "note that some ADRs and recent listings appear late."
            )
        payload = self.http.get_json(f"{DATA}/submissions/CIK{cik}.json")
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        if not forms:
            return []

        columns = (
            "accessionNumber",
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "primaryDocument",
        )
        out: list[dict[str, Any]] = []
        for idx, form in enumerate(forms):
            if str(form).strip() != "4":
                continue
            row = {key: _at(recent.get(key), idx) for key in columns}
            filed_on = _as_date(row.get("filingDate"))
            if filed_on is None or filed_on < since:
                continue
            row["cik"] = cik
            row["issuer_name"] = payload.get("name", "")
            out.append(row)
        return out

    def fetch_transactions(
        self, ticker: str, filing: dict[str, Any]
    ) -> list[InsiderTransaction]:
        accession = str(filing["accessionNumber"])
        nodash = accession.replace("-", "")
        cik_int = str(int(filing["cik"]))
        base = f"{WWW}/Archives/edgar/data/{cik_int}/{nodash}"
        index_url = f"{base}/{accession}-index.htm"

        primary = str(filing.get("primaryDocument") or "")
        candidates: list[str] = []
        if primary:
            # The submissions feed usually points at the XSL-rendered view; the
            # machine-readable XML sits at the same name one level up.
            candidates.append(XSL_PREFIX.sub("", primary))
        xml_text: str | None = None
        for candidate in candidates:
            try:
                xml_text = self.http.get(f"{base}/{candidate}").text
                break
            except ProviderError as exc:
                if exc.status != 404:
                    raise
        if xml_text is None:
            xml_text = self._find_xml_via_index(base)
        if xml_text is None:
            log.warning("no Form 4 XML found for %s (%s)", ticker, accession)
            return []

        filed_at = _as_datetime(filing.get("acceptanceDateTime")) or datetime.now(
            timezone.utc
        )
        return parse_form4(
            xml_text,
            fallback_ticker=ticker,
            accession=accession,
            filed_at=filed_at,
            url=index_url,
        )

    def _find_xml_via_index(self, base: str) -> str | None:
        """Fall back to the filing's directory listing to locate the XML."""
        try:
            listing = self.http.get_json(f"{base}/index.json")
        except ProviderError:
            return None
        items = ((listing or {}).get("directory") or {}).get("item") or []
        names = [
            str(i.get("name", ""))
            for i in items
            if str(i.get("name", "")).lower().endswith(".xml")
        ]
        # Prefer the ownership document over any XBRL sidecar.
        names.sort(key=lambda n: (0 if "form4" in n.lower() or n.lower().startswith("wf-") else 1, n))
        for name in names:
            try:
                text = self.http.get(f"{base}/{name}").text
            except ProviderError:
                continue
            if "ownershipDocument" in text:
                return text
        return None

    def close(self) -> None:
        self.http.close()


# --------------------------------------------------------------------------
# Form 4 XML parsing
# --------------------------------------------------------------------------
def parse_form4(
    xml_text: str,
    *,
    fallback_ticker: str,
    accession: str,
    filed_at: datetime,
    url: str | None = None,
) -> list[InsiderTransaction]:
    """Parse an ownershipDocument into one record per transaction line.

    Form 345 XML carries no namespace, which keeps this mercifully simple.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ProviderError(f"malformed Form 4 XML in {accession}: {exc}") from exc

    issuer_name = _val(root, "issuer/issuerName") or ""
    symbol = (_val(root, "issuer/issuerTradingSymbol") or fallback_ticker).upper()

    owners = root.findall("reportingOwner")
    if owners:
        owner = owners[0]
        insider_name = _val(owner, "reportingOwnerId/rptOwnerName") or "Unknown insider"
        rel = owner.find("reportingOwnerRelationship")
        if len(owners) > 1:
            insider_name += f" (+{len(owners) - 1} co-filer)"
    else:
        insider_name, rel = "Unknown insider", None

    is_director = _flag(rel, "isDirector")
    is_officer = _flag(rel, "isOfficer")
    is_ten_pct = _flag(rel, "isTenPercentOwner")
    officer_title = (_val(rel, "officerTitle") if rel is not None else "") or ""

    doc_10b5 = _has_10b5_marker(root)
    footnotes = " ".join(t.strip() for t in _all_text(root.find("footnotes")))
    if "10b5-1" in footnotes:
        doc_10b5 = True

    out: list[InsiderTransaction] = []
    for table, is_derivative in (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    ):
        for node in root.findall(table):
            shares = _float(_val(node, "transactionAmounts/transactionShares"))
            if shares is None:
                continue
            code = (_val(node, "transactionCoding/transactionCode") or "").strip().upper()
            out.append(
                InsiderTransaction(
                    ticker=symbol,
                    issuer_name=issuer_name,
                    insider_name=insider_name,
                    insider_title=officer_title,
                    is_director=is_director,
                    is_officer=is_officer,
                    is_ten_pct_owner=is_ten_pct,
                    transaction_code=code,
                    acquired_disposed=(
                        _val(node, "transactionAmounts/transactionAcquiredDisposedCode")
                        or ""
                    ).strip().upper(),
                    shares=shares,
                    price_per_share=_float(
                        _val(node, "transactionAmounts/transactionPricePerShare")
                    )
                    or 0.0,
                    transaction_date=(_val(node, "transactionDate") or "")[:10],
                    filed_at=filed_at,
                    accession=accession,
                    is_derivative=is_derivative,
                    is_10b5_1=doc_10b5 or _has_10b5_marker(node),
                    shares_owned_after=_float(
                        _val(
                            node,
                            "postTransactionAmounts/sharesOwnedFollowingTransaction",
                        )
                    ),
                    url=url,
                )
            )
    return out


def category_for(code: str) -> str:
    return CODE_CATEGORY.get(code.upper(), "other")


def label_for(code: str) -> str:
    return CODE_LABEL.get(code.upper(), f"Transaction code {code or '?'}")


def _val(node: ET.Element | None, path: str) -> str | None:
    """Read a Form 4 field, which may be bare text or wrapped in <value>."""
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        return None
    wrapped = found.find("value")
    text = wrapped.text if wrapped is not None else found.text
    return text.strip() if text else None


def _float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _flag(node: ET.Element | None, path: str) -> bool:
    raw = _val(node, path)
    return str(raw).strip().lower() in {"1", "true", "y", "yes"} if raw else False


def _has_10b5_marker(node: ET.Element) -> bool:
    """Detect the Rule 10b5-1 checkbox.

    The element name for this has varied across schema revisions, so match on
    any tag mentioning 10b5 rather than pinning one spelling.
    """
    for element in node.iter():
        if "10b5" not in element.tag.lower():
            continue
        wrapped = element.find("value")
        raw = (wrapped.text if wrapped is not None else element.text) or ""
        if raw.strip().lower() in {"1", "true", "y", "yes"}:
            return True
    return False


def _all_text(node: ET.Element | None) -> Iterable[str]:
    if node is None:
        return []
    return [t for t in node.itertext() if t and t.strip()]


def _at(seq: Any, idx: int) -> Any:
    if isinstance(seq, list) and idx < len(seq):
        return seq[idx]
    return None


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        parsed = _as_date(text)
        if parsed is None:
            return None
        dt = datetime.combine(parsed, datetime.min.time())
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def default_since(lookback_days: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()


PLACEHOLDER_DOMAINS = ("example.com", "example.org", "you@", "your.email", "changeme")


def is_placeholder_user_agent(user_agent: str) -> bool:
    """True for the shipped example address, which must not reach SEC."""
    lowered = user_agent.lower()
    return any(token in lowered for token in PLACEHOLDER_DOMAINS)
