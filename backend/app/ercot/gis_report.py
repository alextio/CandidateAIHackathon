"""Discover and download the latest GIS Report from ERCOT's public MIS.

The Generator Interconnection Status (GIS) Report is data product PG7-200-ER,
report type 15933. It is a monthly .xlsx published on ERCOT's public MIS and
requires no authentication.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

GIS_REPORT_TYPE_ID = 15933
LISTING_URL = "https://www.ercot.com/misapp/GetReports.do"
DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload"

# e.g. RPT.00015933.0000000000000000.20260803.153950829.GIS_Report_July2026.xlsx
_FILE_RE = re.compile(
    r"(RPT\.[0-9.]*?(\d{8})\.\d+\.(GIS_Report_[A-Za-z0-9]+\.xlsx))", re.IGNORECASE
)
_DOCID_RE = re.compile(r"doclookupId=(\d+)")


@dataclass
class GisDocument:
    doc_lookup_id: str
    file_name: str
    posted_date: date  # date the file was posted (from the RPT.* name)


def list_gis_documents(client: httpx.Client) -> list[GisDocument]:
    """Return every posted GIS Report document, newest first."""
    resp = client.get(
        LISTING_URL, params={"reportTypeId": GIS_REPORT_TYPE_ID}, timeout=30
    )
    resp.raise_for_status()
    html = resp.text

    docs: list[GisDocument] = []
    # Each row pairs a file name with its own doclookupId in the download link.
    for row in re.split(r"</tr>", html):
        fm = _FILE_RE.search(row)
        dm = _DOCID_RE.search(row)
        if fm and dm:
            posted = datetime.strptime(fm.group(2), "%Y%m%d").date()
            docs.append(
                GisDocument(
                    doc_lookup_id=dm.group(1),
                    file_name=fm.group(3),
                    posted_date=posted,
                )
            )
    docs.sort(key=lambda d: d.posted_date, reverse=True)
    return docs


def latest_gis_document(client: httpx.Client) -> GisDocument:
    docs = list_gis_documents(client)
    if not docs:
        raise RuntimeError("No GIS Report documents found on ERCOT MIS listing.")
    return docs[0]


def recent_gis_documents(client: httpx.Client, months: int = 12) -> list[GisDocument]:
    """Return the most recent `months` GIS Report documents, newest first.

    De-duplicates by report month (a month occasionally has multiple postings),
    keeping the newest posting for each.
    """
    docs = list_gis_documents(client)
    seen: dict[date, GisDocument] = {}
    for doc in docs:  # already newest-first
        rd = report_date_from_name(doc.file_name) or doc.posted_date
        if rd not in seen:
            seen[rd] = doc
    ordered = sorted(seen.values(), key=lambda d: d.posted_date, reverse=True)
    return ordered[:months]


def download_document(client: httpx.Client, doc: GisDocument) -> bytes:
    resp = client.get(
        DOWNLOAD_URL,
        params={"mimic_duns": "000000000", "doclookupId": doc.doc_lookup_id},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


# Report month parsed from the file name. ERCOT mixes full and abbreviated month
# names across postings, e.g. "GIS_Report_July2026.xlsx", "GIS_Report_Jun2026.xlsx".
_FULL_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTHS: dict[str, int] = {}
for _i, _name in enumerate(_FULL_MONTHS, start=1):
    _MONTHS[_name.lower()] = _i
    _MONTHS[_name[:3].lower()] = _i  # 3-letter abbreviation (Jan, Jun, Sep, ...)


def report_date_from_name(file_name: str) -> date | None:
    m = re.search(r"GIS_Report_([A-Za-z]+)(\d{4})", file_name)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return date(int(m.group(2)), month, 1)
