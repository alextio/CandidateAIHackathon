"""The Supabase layer, against a fake client.

Two failures matter here and neither shows up as an exception: a read silently
truncated at PostgREST's 1,000-row cap, and a NaN that makes the whole upsert
batch fail. Both are covered.
"""
from __future__ import annotations

import math
from datetime import date

from app.classify import store

AS_OF = date(2026, 7, 1)


class FakeQuery:
    """Records the filters applied, returns whatever the table was seeded with."""

    def __init__(self, table: "FakeTable") -> None:
        self.table = table
        self.filters: list[tuple] = []
        self._range: tuple[int, int] | None = None

    def _record(self, name, *args):
        self.filters.append((name, *args))
        return self

    select = lambda self, *a, **k: self._record("select", *a)  # noqa: E731
    eq = lambda self, *a: self._record("eq", *a)  # noqa: E731
    lte = lambda self, *a: self._record("lte", *a)  # noqa: E731
    gte = lambda self, *a: self._record("gte", *a)  # noqa: E731
    in_ = lambda self, *a: self._record("in", *a)  # noqa: E731
    limit = lambda self, *a: self._record("limit", *a)  # noqa: E731

    def order(self, column, desc=False):
        return self._record("order", column, desc)

    def is_(self, *args):
        return self._record("is", *args)

    @property
    def not_(self):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = self.table.rows
        if self._range is not None:
            start, end = self._range
            rows = rows[start : end + 1]
        return type("Response", (), {"data": rows, "count": len(self.table.rows)})()


class FakeTable:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.upserts: list[tuple[list[dict], str]] = []
        self.last_query: FakeQuery | None = None

    def select(self, *args, **kwargs):
        self.last_query = FakeQuery(self)
        return self.last_query.select(*args)

    def upsert(self, rows, on_conflict=""):
        self.upserts.append((rows, on_conflict))
        return type("Executable", (), {"execute": lambda _self: None})()


class FakeClient:
    def __init__(self, **tables) -> None:
        self.tables = {name: FakeTable(rows) for name, rows in tables.items()}

    def table(self, name):
        return self.tables.setdefault(name, FakeTable())


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def test_reads_page_past_the_thousand_row_cap():
    """A silent truncation would train the model on the alphabetical first tenth."""
    rows = [{"inr": f"26INR{index:05d}", "report_date": "2026-07-01"} for index in range(2500)]
    client = FakeClient(ercot_projects=rows)

    fetched = store.fetch_snapshots(client)

    assert len(fetched) == 2500


def test_as_of_is_pushed_down_to_the_query_not_applied_after_the_fetch():
    """The leakage guard has to run server-side.

    Filtering after the fetch would still be correct, but it would pull future
    snapshots across the wire and make a backtest quietly expensive.
    """
    client = FakeClient(ercot_projects=[])

    store.fetch_snapshots(client, as_of=AS_OF)

    filters = client.tables["ercot_projects"].last_query.filters
    assert ("lte", "report_date", "2026-07-01") in filters


def test_latest_by_inr_keeps_the_newest_snapshot_per_project():
    snapshots = [
        {"inr": "A", "report_date": "2026-05-01", "capacity_mw": 1},
        {"inr": "A", "report_date": "2026-07-01", "capacity_mw": 2},
        {"inr": "B", "report_date": "2026-06-01", "capacity_mw": 3},
    ]

    latest = {row["inr"]: row for row in store.latest_by_inr(snapshots)}

    assert len(latest) == 2
    assert latest["A"]["capacity_mw"] == 2
    assert latest["B"]["capacity_mw"] == 3


def test_latest_by_inr_ignores_rows_with_no_project_id():
    assert store.latest_by_inr([{"inr": None, "report_date": "2026-07-01"}]) == []


def test_linked_permits_are_keyed_by_project_and_carry_the_link_score():
    client = FakeClient(
        resolved_links=[
            {"inr": "26INR0001", "rn_number": "RN1", "permit_no": "P1", "score": 0.9},
            {"inr": "26INR0002", "rn_number": "RN2", "permit_no": "P2", "score": 0.5},
        ],
        tceq_permits=[
            {"rn_number": "RN1", "permit_no": "P1", "snapshot_date": "2026-06-01"},
            # Newer snapshot of the same permit must win.
            {"rn_number": "RN1", "permit_no": "P1", "snapshot_date": "2026-07-01"},
            {"rn_number": "RN2", "permit_no": "P2", "snapshot_date": "2026-07-01"},
        ],
    )

    linked = store.fetch_linked_permits(client)

    assert set(linked) == {"26INR0001", "26INR0002"}
    assert len(linked["26INR0001"]) == 1
    assert linked["26INR0001"][0]["snapshot_date"] == "2026-07-01"
    assert linked["26INR0001"][0]["link_score"] == 0.9


def test_no_links_means_no_permit_lookup_at_all():
    client = FakeClient(resolved_links=[], tceq_permits=[{"rn_number": "RN1"}])

    assert store.fetch_linked_permits(client) == {}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def test_nan_becomes_null_so_one_unestimable_project_cannot_fail_the_batch():
    """Momentum emits NaN where a slope could not be fitted. JSON has no NaN."""
    client = FakeClient()

    store.upsert_momentum(
        client,
        [{"inr": "A", "as_of": AS_OF, "cod_slip_rate": math.nan, "phase_velocity": 1.5}],
    )

    rows, on_conflict = client.tables["project_momentum"].upserts[0]
    assert rows[0]["cod_slip_rate"] is None
    assert rows[0]["phase_velocity"] == 1.5
    assert on_conflict == "inr,as_of"


def test_dates_are_serialised_as_iso_strings():
    client = FakeClient()

    store.upsert_momentum(client, [{"inr": "A", "as_of": AS_OF}])

    rows, _ = client.tables["project_momentum"].upserts[0]
    assert rows[0]["as_of"] == "2026-07-01"


def test_predictions_upsert_on_the_full_prediction_key():
    client = FakeClient()

    written = store.upsert_predictions(
        client, [{"entity_id": "ercot:A", "as_of": AS_OF, "model_version": "v1"}]
    )

    _, on_conflict = client.tables["stage_predictions"].upserts[0]
    assert written == 1
    assert on_conflict == "entity_id,as_of,model_version"


def test_writing_nothing_touches_no_table():
    client = FakeClient()

    assert store.upsert_predictions(client, []) == 0
    assert store.upsert_momentum(client, []) == 0
    assert client.tables == {}


def test_large_writes_are_batched():
    client = FakeClient()
    rows = [{"inr": str(index), "as_of": AS_OF} for index in range(1200)]

    written = store.upsert_momentum(client, rows)

    batches = client.tables["project_momentum"].upserts
    assert written == 1200
    assert len(batches) == 3
    assert sum(len(batch) for batch, _ in batches) == 1200


# ---------------------------------------------------------------------------
# The two sources date themselves differently
#
# `ercot_projects.report_date` is the month a queue snapshot describes.
# `tceq_permits.snapshot_date` is the day this pipeline crawled the registry.
# Filtering the second by the first matches nothing — the crawl is always newer
# than any published report month — which silently emptied every permit feature
# and made the FID stage unlabelable.
# ---------------------------------------------------------------------------
def _permit_filters(client) -> list[tuple]:
    return client.tables["tceq_permits"].last_query.filters


def _linked_client():
    return FakeClient(
        resolved_links=[{"inr": "26INR0001", "rn_number": "RN1", "permit_no": "P1", "score": 0.9}],
        tceq_permits=[{"rn_number": "RN1", "permit_no": "P1", "snapshot_date": "2026-08-08"}],
    )


def test_permits_are_not_cut_off_by_a_crawl_date_when_scoring_the_present():
    client = _linked_client()

    linked = store.fetch_linked_permits(client)

    assert set(linked) == {"26INR0001"}
    assert not any(name == "lte" for name, *_ in _permit_filters(client))


def test_an_explicit_backtest_still_pushes_the_crawl_cut_off_down():
    client = _linked_client()

    store.fetch_linked_permits(client, as_of=AS_OF)

    assert ("lte", "snapshot_date", "2026-07-01") in _permit_filters(client)


def test_default_run_does_not_filter_permits_by_the_ercot_report_month():
    """The regression, at the level where it actually happened."""
    from app.classify import service

    client = FakeClient(
        ercot_projects=[
            {
                "inr": "26INR0001",
                "report_date": "2026-07-01",
                "status": "active",
                "gim_study_phase": "SS Completed, FIS Started, No IA",
                "projected_cod": "2028-06-01",
                "capacity_mw": 250.0,
                "ia_signed": None,
            }
        ],
        resolved_links=[
            {"inr": "26INR0001", "rn_number": "RN1", "permit_no": "P1", "score": 0.9}
        ],
        tceq_permits=[
            {
                "rn_number": "RN1",
                "permit_no": "P1",
                # Crawled after the newest report month, as every crawl is.
                "snapshot_date": "2026-08-08",
                "entity_status": "active",
                "affiliation_begin_date": "2025-03-01",
                "affiliation_end_date": None,
                "on_thesis": True,
            }
        ],
    )

    context = service.load_context(client)

    assert context.as_of == AS_OF
    assert context.permits["26INR0001"], "the permit was dropped by the crawl-date filter"
    assert not any(name == "lte" for name, *_ in _permit_filters(client))
    assert context.frame["n_permits"].to_list() == [1.0]
    assert context.frame["has_active_permit"].to_list() == [True]
