# database/events.py
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping

from .connection import get_connection


def _event_get(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _normalize_event_for_db(event: Any) -> SimpleNamespace:
    event_id = _event_get(event, "id")
    if event_id is None:
        raise ValueError("event id is required for insert_event")

    name = (
        _event_get(event, "name")
        or _event_get(event, "title")
        or _event_get(event, "slug")
        or str(event_id)
    )
    description = _event_get(event, "description")
    created_at = (
        _event_get(event, "created_at")
        or _event_get(event, "createdAt")
        or datetime.utcnow()
    )
    # An event is considered active only if it is not closed.
    # The Gamma API's own `active` field is unreliable (returns True even for
    # resolved/closed events), so we use `closed=False` as the definitive signal.
    closed = bool(_event_get(event, "closed", False))
    active = not closed
    return SimpleNamespace(
        id=str(event_id),
        name=str(name),
        description=str(description) if description is not None else None,
        created_at=created_at,
        active=active,
    )


def get_events():
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM events;")
        return cur.fetchall()   # list[dict]


def get_all_event_ids() -> list[str]:
    """Return IDs of all active events stored in the database, sorted ascending."""
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM events WHERE active = TRUE ORDER BY id;")
        rows = cur.fetchall()
    return [str(row["id"]) for row in rows]


def get_event(event_id: str):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM events WHERE id = %s;", (event_id,))
        return cur.fetchone()


def insert_event(event):
    normalized = _normalize_event_for_db(event)
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (id, name, description, created_at, active)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE
            SET
              name = EXCLUDED.name,
              description = EXCLUDED.description,
              created_at = EXCLUDED.created_at,
              active = EXCLUDED.active;
            """,
            (
                normalized.id,
                normalized.name,
                normalized.description,
                normalized.created_at,
                normalized.active,
            ),
        )
        conn.commit()



def insert_whale_spike(spike, market_id: str | None = None):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO whale_spikes (
              event_id, market_id, from_ts, to_ts, side,
              from_price, to_price, abs_change, rel_change
            )
            SELECT
              %s,%s,%s,%s,%s,%s,%s,%s,%s
            WHERE NOT EXISTS (
              SELECT 1
              FROM whale_spikes ws
              WHERE ws.event_id = %s
                AND ws.market_id IS NOT DISTINCT FROM %s
                AND ws.from_ts = %s
                AND ws.to_ts = %s
                AND ws.side = %s
            );
            """,
            (
                spike.event_id,
                market_id,
                spike.from_ts,
                spike.to_ts,
                spike.side,
                spike.from_price,
                spike.to_price,
                spike.abs_change,
                spike.rel_change,
                spike.event_id,
                market_id,
                spike.from_ts,
                spike.to_ts,
                spike.side,
            ),
        )
        conn.commit() 



def update_market_volume(market_id: str, volume: float):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO markets (id, volume_usd)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE
            SET volume_usd = EXCLUDED.volume_usd;
            """,
            (market_id, volume),
        )
        conn.commit()


def get_recent_whale_spikes(event_id: str, limit: int = 5):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              event_id,
              market_id,
              from_ts,
              to_ts,
              side,
              from_price,
              to_price,
              abs_change,
              rel_change
            FROM whale_spikes
            WHERE event_id = %s
            ORDER BY to_ts DESC
            LIMIT %s;
            """,
            (event_id, max(1, min(int(limit), 100))),
        )
        return cur.fetchall()
