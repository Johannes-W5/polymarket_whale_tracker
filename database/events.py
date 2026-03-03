# database/events.py
from contextlib import closing
from .connection import get_connection

def get_events():
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM events;")
        return cur.fetchall()   # list[dict]


def get_event(event_id: str):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM events WHERE id = %s;", (event_id,))
        return cur.fetchone()


def insert_event(event):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (id, name, description, created_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE
            SET
              name = EXCLUDED.name,
              description = EXCLUDED.description,
              created_at = EXCLUDED.created_at;
            """,
            (event.id, event.name, event.description, event.created_at),
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
