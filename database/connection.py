# database/connection.py
"""PostgreSQL connections for local dev and Render (DATABASE_URL)."""

from __future__ import annotations

import os

import psycopg2
from psycopg2.extras import RealDictCursor


def _connect_from_database_url(url: str):
    """Connect using a libpq URL (Render sets postgresql://... on private network)."""
    cleaned = url.strip()
    if cleaned.startswith("postgres://"):
        cleaned = "postgresql://" + cleaned[len("postgres://") :]
    return psycopg2.connect(cleaned, cursor_factory=RealDictCursor)


def get_connection():
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if database_url:
        return _connect_from_database_url(database_url)

    dbname = (os.getenv("PG_DB") or "").strip()
    user = (os.getenv("PG_USER") or "").strip()
    password = os.getenv("PG_PASSWORD")
    host = (os.getenv("PG_HOST") or "").strip()
    port_raw = (os.getenv("PG_PORT") or "5432").strip()

    missing = [
        name
        for name, val in (
            ("PG_DB", dbname),
            ("PG_USER", user),
            ("PG_PASSWORD", password),
            ("PG_HOST", host),
        )
        if val is None or (isinstance(val, str) and not val.strip())
    ]
    if missing:
        raise RuntimeError(
            "Database configuration missing: set DATABASE_URL (recommended on Render) "
            "or all of PG_DB, PG_USER, PG_PASSWORD, PG_HOST. "
            f"Missing or empty: {', '.join(missing)}"
        )

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid PG_PORT: {port_raw!r}") from exc

    return psycopg2.connect(
        dbname=dbname,
        user=user,
        password=password,
        host=host,
        port=port,
        cursor_factory=RealDictCursor,
    )

