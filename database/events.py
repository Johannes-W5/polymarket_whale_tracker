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


def _is_event_active(event: Any) -> bool:
    """
    Treat an event as monitorable only when it is explicitly active and not closed.

    Gamma's `active` flag alone is not sufficient because some closed/resolved
    events still report `active=True`, but `active=False` should still exclude
    the event from storage/monitoring.
    """
    active_raw = _event_get(event, "active")
    closed_raw = _event_get(event, "closed", False)
    return bool(active_raw) and not bool(closed_raw)


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
    active = _is_event_active(event)
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


def _ensure_insider_assessments_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS insider_assessments (
          id BIGSERIAL PRIMARY KEY,
          event_id TEXT NOT NULL,
          trigger_type TEXT NOT NULL,
          market_id TEXT NULL,
          side TEXT NULL,
          from_ts TIMESTAMPTZ NULL,
          to_ts TIMESTAMPTZ NULL,
          from_price DOUBLE PRECISION NULL,
          to_price DOUBLE PRECISION NULL,
          abs_change DOUBLE PRECISION NULL,
          rel_change DOUBLE PRECISION NULL,
          probability_insider DOUBLE PRECISION NOT NULL,
          confidence TEXT NOT NULL,
          short_summary TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def insert_insider_assessment(
    *,
    event_id: str,
    trigger_type: str,
    spike,
    assessment,
    market_id: str | None = None,
):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_insider_assessments_table(cur)
        cur.execute(
            """
            INSERT INTO insider_assessments (
              event_id,
              trigger_type,
              market_id,
              side,
              from_ts,
              to_ts,
              from_price,
              to_price,
              abs_change,
              rel_change,
              probability_insider,
              confidence,
              short_summary
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                str(event_id),
                str(trigger_type),
                market_id,
                getattr(spike, "side", None),
                getattr(spike, "from_ts", None),
                getattr(spike, "to_ts", None),
                getattr(spike, "from_price", None),
                getattr(spike, "to_price", None),
                getattr(spike, "abs_change", None),
                getattr(spike, "rel_change", None),
                float(getattr(assessment, "probability_insider")),
                str(getattr(assessment, "confidence")),
                str(getattr(assessment, "short_summary")),
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


def get_latest_whale_spikes(limit: int = 20):
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
            ORDER BY to_ts DESC
            LIMIT %s;
            """,
            (max(1, min(int(limit), 100)),),
        )
        return cur.fetchall()


def get_latest_assessment_for_event(event_id: str):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_insider_assessments_table(cur)
        cur.execute(
            """
            SELECT
              event_id,
              trigger_type,
              market_id,
              side,
              from_ts,
              to_ts,
              from_price,
              to_price,
              abs_change,
              rel_change,
              probability_insider,
              confidence,
              short_summary,
              created_at
            FROM insider_assessments
            WHERE event_id = %s
            ORDER BY created_at DESC, to_ts DESC NULLS LAST
            LIMIT 1;
            """,
            (event_id,),
        )
        return cur.fetchone()
