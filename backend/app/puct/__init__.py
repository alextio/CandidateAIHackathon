"""PUCT Interchange docket discovery (Source #3).

Scrapes the server-rendered PUCT Interchange filing search for a curated set of
large-load / interconnection dockets, normalizes each filing into a typed record,
derives a small set of dated regulatory milestones (the highest-confidence
"getting real" signals in the pipeline), and resolves the filing parties to
ERCOT queue projects and TCEQ air permits. Metadata-only in v1 — no PDF parsing.
"""
