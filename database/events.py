# database/events.py
from contextlib import closing
from datetime import datetime
import json
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



def _ensure_market_reference(cur, market_id: str | None, *, volume: float | None = None) -> None:
    """
    Ensure a minimal `markets` row exists before inserting rows with a market FK.
    """
    if market_id is None:
        return
    cur.execute(
        """
        INSERT INTO markets (id, volume_usd)
        VALUES (%s, %s)
        ON CONFLICT (id) DO UPDATE
        SET volume_usd = COALESCE(EXCLUDED.volume_usd, markets.volume_usd);
        """,
        (str(market_id), volume),
    )


def insert_whale_spike(spike, market_id: str | None = None):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_market_reference(
            cur,
            market_id,
            volume=getattr(spike, "market_volume", None),
        )
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
          spike_id TEXT NULL,
          market_id TEXT NULL,
          side TEXT NULL,
          from_ts TIMESTAMPTZ NULL,
          to_ts TIMESTAMPTZ NULL,
          signal_time TIMESTAMPTZ NULL,
          news_time TIMESTAMPTZ NULL,
          news_delta_minutes DOUBLE PRECISION NULL,
          from_price DOUBLE PRECISION NULL,
          to_price DOUBLE PRECISION NULL,
          abs_change DOUBLE PRECISION NULL,
          rel_change DOUBLE PRECISION NULL,
          deterministic_score DOUBLE PRECISION NULL,
          deterministic_score_band TEXT NULL,
          deterministic_feature_snapshot JSONB NULL,
          scorer_version TEXT NULL,
          probability_insider DOUBLE PRECISION NULL,
          confidence TEXT NULL,
          short_summary TEXT NULL,
          llm_version TEXT NULL,
          prompt_hash TEXT NULL,
          trigger_payload JSONB NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS spike_id TEXT NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS signal_time TIMESTAMPTZ NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS news_time TIMESTAMPTZ NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS news_delta_minutes DOUBLE PRECISION NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS deterministic_score DOUBLE PRECISION NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS deterministic_score_band TEXT NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS deterministic_feature_snapshot JSONB NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS scorer_version TEXT NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS llm_version TEXT NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS prompt_hash TEXT NULL;")
    cur.execute("ALTER TABLE insider_assessments ADD COLUMN IF NOT EXISTS trigger_payload JSONB NULL;")
    cur.execute("ALTER TABLE insider_assessments ALTER COLUMN probability_insider DROP NOT NULL;")
    cur.execute("ALTER TABLE insider_assessments ALTER COLUMN confidence DROP NOT NULL;")
    cur.execute("ALTER TABLE insider_assessments ALTER COLUMN short_summary DROP NOT NULL;")


def _ensure_cross_asset_predictions_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cross_asset_predictions (
          id BIGSERIAL PRIMARY KEY,
          assessment_id BIGINT NULL,
          event_id TEXT NOT NULL,
          spike_id TEXT NULL,
          asset_symbol TEXT NOT NULL,
          asset_class TEXT NOT NULL,
          horizon_bucket TEXT NOT NULL,
          predicted_direction TEXT NOT NULL,
          predicted_magnitude_band TEXT NOT NULL,
          prediction_confidence DOUBLE PRECISION NOT NULL,
          rationale_components JSONB NOT NULL,
          model_version TEXT NOT NULL,
          source_score DOUBLE PRECISION NULL,
          source_score_band TEXT NULL,
          signal_time TIMESTAMPTZ NULL,
          metadata JSONB NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_cross_asset_predictions_identity
        ON cross_asset_predictions (
          COALESCE(assessment_id, -1),
          event_id,
          COALESCE(spike_id, ''),
          asset_symbol,
          horizon_bucket,
          model_version
        );
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_cross_asset_predictions_event_created ON cross_asset_predictions (event_id, created_at DESC);"
    )


def insert_insider_assessment(
    *,
    event_id: str,
    trigger_type: str,
    spike,
    assessment=None,
    market_id: str | None = None,
    trigger_payload: dict[str, Any] | None = None,
) -> int | None:
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_insider_assessments_table(cur)
        _ensure_market_reference(
            cur,
            market_id,
            volume=getattr(spike, "market_volume", None),
        )
        cur.execute(
            """
            INSERT INTO insider_assessments (
              event_id,
              trigger_type,
              spike_id,
              market_id,
              side,
              from_ts,
              to_ts,
              signal_time,
              news_time,
              news_delta_minutes,
              from_price,
              to_price,
              abs_change,
              rel_change,
              deterministic_score,
              deterministic_score_band,
              deterministic_feature_snapshot,
              scorer_version,
              probability_insider,
              confidence,
              short_summary,
              llm_version,
              prompt_hash,
              trigger_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id;
            """,
            (
                str(event_id),
                str(trigger_type),
                getattr(spike, "spike_id", None),
                market_id,
                getattr(spike, "side", None),
                getattr(spike, "from_ts", None),
                getattr(spike, "to_ts", None),
                getattr(spike, "signal_time", None),
                getattr(spike, "news_time", None),
                getattr(spike, "news_delta_minutes", None),
                getattr(spike, "from_price", None),
                getattr(spike, "to_price", None),
                getattr(spike, "abs_change", None),
                getattr(spike, "rel_change", None),
                getattr(spike, "deterministic_score", None),
                getattr(spike, "deterministic_score_band", None),
                json.dumps(getattr(spike, "deterministic_feature_snapshot", None)),
                getattr(spike, "scorer_version", None),
                (
                    float(getattr(assessment, "probability_insider"))
                    if getattr(assessment, "probability_insider", None) is not None
                    else None
                ),
                getattr(assessment, "confidence", None),
                getattr(assessment, "short_summary", None),
                getattr(assessment, "llm_version", None),
                getattr(assessment, "prompt_hash", None),
                json.dumps(trigger_payload) if trigger_payload is not None else None,
            ),
        )
        inserted = cur.fetchone()
        conn.commit()
        if isinstance(inserted, Mapping) and inserted.get("id") is not None:
            return int(inserted["id"])
        return None



def insert_cross_asset_prediction(
    *,
    assessment_id: int | None,
    event_id: str,
    spike_id: str | None,
    asset_symbol: str,
    asset_class: str,
    horizon_bucket: str,
    predicted_direction: str,
    predicted_magnitude_band: str,
    prediction_confidence: float,
    rationale_components: list[dict[str, Any]] | dict[str, Any],
    model_version: str,
    source_score: float | None = None,
    source_score_band: str | None = None,
    signal_time: datetime | None = None,
    metadata: dict[str, Any] | None = None,
):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_cross_asset_predictions_table(cur)
        cur.execute(
            """
            INSERT INTO cross_asset_predictions (
              assessment_id,
              event_id,
              spike_id,
              asset_symbol,
              asset_class,
              horizon_bucket,
              predicted_direction,
              predicted_magnitude_band,
              prediction_confidence,
              rationale_components,
              model_version,
              source_score,
              source_score_band,
              signal_time,
              metadata
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb
            WHERE NOT EXISTS (
              SELECT 1
              FROM cross_asset_predictions cap
              WHERE cap.assessment_id IS NOT DISTINCT FROM %s
                AND cap.event_id = %s
                AND cap.spike_id IS NOT DISTINCT FROM %s
                AND cap.asset_symbol = %s
                AND cap.horizon_bucket = %s
                AND cap.model_version = %s
            );
            """,
            (
                assessment_id,
                str(event_id),
                spike_id,
                str(asset_symbol).upper(),
                str(asset_class).lower(),
                str(horizon_bucket).lower(),
                str(predicted_direction).lower(),
                str(predicted_magnitude_band).lower(),
                float(max(0.0, min(1.0, prediction_confidence))),
                json.dumps(rationale_components),
                str(model_version),
                source_score,
                source_score_band,
                signal_time,
                json.dumps(metadata) if metadata is not None else None,
                assessment_id,
                str(event_id),
                spike_id,
                str(asset_symbol).upper(),
                str(horizon_bucket).lower(),
                str(model_version),
            ),
        )
        conn.commit()


def get_latest_cross_asset_predictions_for_event(event_id: str, limit: int = 20):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_cross_asset_predictions_table(cur)
        cur.execute(
            """
            SELECT
              id,
              assessment_id,
              event_id,
              spike_id,
              asset_symbol,
              asset_class,
              horizon_bucket,
              predicted_direction,
              predicted_magnitude_band,
              prediction_confidence,
              rationale_components,
              model_version,
              source_score,
              source_score_band,
              signal_time,
              metadata,
              created_at
            FROM cross_asset_predictions
            WHERE event_id = %s
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (event_id, max(1, min(int(limit), 250))),
        )
        return cur.fetchall()


def get_high_score_assessments(
    *,
    min_score: float = 70.0,
    since_id: int | None = None,
    limit: int = 500,
):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        _ensure_insider_assessments_table(cur)
        params: list[Any] = [float(min_score)]
        where = ["deterministic_score >= %s", "trigger_payload IS NOT NULL"]
        if since_id is not None:
            where.append("id > %s")
            params.append(int(since_id))
        params.append(max(1, min(int(limit), 5000)))
        cur.execute(
            f"""
            SELECT
              id,
              event_id,
              trigger_type,
              spike_id,
              side,
              signal_time,
              deterministic_score,
              deterministic_score_band,
              deterministic_feature_snapshot,
              trigger_payload,
              created_at
            FROM insider_assessments
            WHERE {" AND ".join(where)}
            ORDER BY id ASC
            LIMIT %s;
            """,
            tuple(params),
        )
        return cur.fetchall()


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
              spike_id,
              market_id,
              side,
              from_ts,
              to_ts,
              signal_time,
              news_time,
              news_delta_minutes,
              from_price,
              to_price,
              abs_change,
              rel_change,
              deterministic_score,
              deterministic_score_band,
              deterministic_feature_snapshot,
              scorer_version,
              probability_insider,
              probability_insider AS llm_probability,
              confidence,
              confidence AS llm_confidence,
              short_summary,
              short_summary AS llm_summary,
              llm_version,
              prompt_hash,
              trigger_payload,
              created_at
            FROM insider_assessments
            WHERE event_id = %s
            ORDER BY created_at DESC, to_ts DESC NULLS LAST
            LIMIT 1;
            """,
            (event_id,),
        )
        return cur.fetchone()
