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
            VALUES (%s,%s,%s,%s);
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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s);
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
            ),
        )
        conn.commit() 



def update_market_volume(market_id: str, volume: float):
    with closing(get_connection()) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE markets SET volume_usd = %s WHERE id = %s;",
            (volume, market_id),
        )
        conn.commit()
