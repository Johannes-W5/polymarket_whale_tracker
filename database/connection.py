# database/connection.py
import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_connection():
    return psycopg2.connect(
        dbname=os.getenv("PG_DB", "polymarket_sentiment"),
        user=os.getenv("PG_USER", "polymarket_user"),
        password=os.getenv("PG_PASSWORD", "your_password"),
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        cursor_factory=RealDictCursor,
    )

