"""Fetch PUCT Interchange docket filings.

Source: the PUCT Interchange Filing Search (interchange.puc.texas.gov). There is
no official API, but the docket route is server-rendered ASP.NET — no JavaScript
and no headless browser required. Verified live 2026-08:

    GET /search/filings/?UtilityType=A&ControlNumber=<N>
        [&FilingParty=<name>] [&DateFiledFrom=M/D/YYYY] [&DateFiledTo=M/D/YYYY]

returns one HTML <table> with a row per filing: item number (linked to that
item's document list), filed date, filing party, item-type code, and filing
description. We scrape that table with the stdlib HTML parser (no new deps).

Note (verified): searches WITHOUT a ControlNumber return a JavaScript shell with
no rows, and POST 404s — so cross-docket "search by company name" is not
reachable over plain HTTP. FilingParty only narrows results *within* a docket.
Entity discovery is therefore done by resolving the parties in our seed dockets,
not by querying Interchange for a company across all dockets.
"""
from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx
import truststore

INTERCHANGE_BASE = "https://interchange.puc.texas.gov"
FILINGS_PATH = "/search/filings/"
# Per-item document listing (what each item-number cell links to on Interchange).
DOCUMENTS_PATH = "/search/documents/"


def make_client() -> httpx.Client:
    """httpx client that trusts the OS certificate store.

    Interchange's chain is valid but roots at an SSL.com CA that ships in the OS
    trust store yet not in certifi (httpx's default), so certifi-based
    verification fails. `truststore` bridges httpx to the platform store, keeping
    full verification without pinning or disabling it.
    """
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.Client(verify=ctx, follow_redirects=True)


@dataclass
class DocketFilings:
    """Raw scraped filing rows for one docket (mirrors TCEQ's RegionRows)."""

    control_number: str
    docket_title: str | None
    rows: list[dict]


class _FilingsTableParser(HTMLParser):
    """Extract the filings table into per-row dicts.

    The results page has exactly one <table> (the filing list). We read its
    header cells to learn the column order, then emit one dict per data row keyed
    by header, plus the item number and its document-list href lifted out.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._is_header_row = False
        self._headers: list[str] = []
        self._cells: list[str] = []
        self._cell_buf: list[str] = []
        self._cell_href: str | None = None
        self._row_href: str | None = None  # first href in the row (item link)
        self.rows: list[dict] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table" and not self._in_table and not self.rows:
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._is_header_row = False
            self._cells = []
            self._row_href = None
        elif self._in_table and tag in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []
            self._cell_href = None
            if tag == "th":
                self._is_header_row = True
        elif self._in_cell and tag == "a":
            for name, value in attrs:
                if name == "href" and self._cell_href is None:
                    self._cell_href = value
                    if self._row_href is None:
                        self._row_href = value

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_table:
            self._in_table = False
        elif self._in_table and tag in ("td", "th"):
            self._in_cell = False
            text = " ".join("".join(self._cell_buf).split())
            self._cells.append(text)
        elif self._in_table and tag == "tr":
            self._in_row = False
            if self._is_header_row:
                self._headers = [c.strip() for c in self._cells]
            elif self._cells:
                self.rows.append(self._build_row())

    def _build_row(self) -> dict:
        row: dict[str, str | None] = {}
        headers = self._headers or [
            "Item Number", "Filed Date", "Filing Party", "Item Type", "Filing Description"
        ]
        for i, value in enumerate(self._cells):
            key = headers[i] if i < len(headers) else f"col_{i}"
            row[key] = value
        row["_item_href"] = self._row_href
        return row


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip()
    return s or None


def _pick(row: dict, *needles: str) -> str | None:
    """Fetch a table cell by fuzzy header match (case/space-insensitive)."""
    for key, value in row.items():
        k = key.lower()
        if all(n in k for n in needles):
            return _clean(value)
    return None


def _extract_docket_title(html: str) -> str | None:
    """Best-effort pull of the docket's case style from the results page."""
    m = re.search(r"Filings\s+for\s+([^<\n]+?)\s*<", html)
    if m:
        title = " ".join(m.group(1).split())
        return title or None
    return None


def parse_filings_html(html: str, control_number: str) -> DocketFilings:
    """Parse a server-rendered filings page into normalized raw rows."""
    parser = _FilingsTableParser()
    parser.feed(html)
    title = _extract_docket_title(html)
    if title == control_number:  # "Filings for <N>" with no case style -> no title
        title = None
    rows: list[dict] = []
    for r in parser.rows:
        href = r.get("_item_href")
        # The table's final row is an "Export to Excel" link, not a filing.
        if href and "exportfilings" in href.lower():
            continue
        # Item cells are integers; anything else (footers, spacers) is dropped.
        # "Item" sorts before "Item Type" in the header order, so _pick hits the
        # bare item-number column first.
        item_cell = _pick(r, "item")
        m = re.match(r"\s*(\d+)", item_cell or "")
        if not m:
            continue
        source_url = f"{INTERCHANGE_BASE}{href}" if href else None
        rows.append(
            {
                "control_number": control_number,
                "item_number": m.group(1),
                "filed_date": _pick(r, "stamp") or _pick(r, "filed") or _pick(r, "date"),
                "filing_party": _pick(r, "party"),
                "item_type": _pick(r, "item", "type") or _pick(r, "type"),
                "filing_description": _pick(r, "description"),
                "source_url": source_url,
                "docket_title": title,
            }
        )
    return DocketFilings(control_number=control_number, docket_title=title, rows=rows)


def fetch_docket(
    client: httpx.Client,
    control_number: str,
    *,
    utility_type: str = "A",
    filing_party: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> DocketFilings:
    """Fetch and parse one docket's filing list from the server-rendered route."""
    params: dict[str, str] = {
        "UtilityType": utility_type,
        "ControlNumber": str(control_number),
    }
    if filing_party:
        params["FilingParty"] = filing_party
    if date_from:
        params["DateFiledFrom"] = date_from
    if date_to:
        params["DateFiledTo"] = date_to
    resp = client.get(f"{INTERCHANGE_BASE}{FILINGS_PATH}", params=params, timeout=60)
    resp.raise_for_status()
    return parse_filings_html(resp.text, str(control_number))


def fetch_dockets(
    client: httpx.Client,
    control_numbers: list[str],
    *,
    utility_type: str = "A",
) -> list[DocketFilings]:
    """Fetch each control number in the seed set, returning raw rows per docket."""
    out: list[DocketFilings] = []
    for cn in control_numbers:
        out.append(fetch_docket(client, cn, utility_type=utility_type))
    return out
