"""
db.py — PostgreSQL connection pool для EMET Bot.
Заменяет sqlite3 во всех модулях проекта.
"""

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


TZ = os.getenv("TZ", "Europe/Kiev")


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.getenv(
            "DATABASE_URL",
            "postgresql://emet:emet2026@localhost:5432/emet_bot"
        )
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 20, url,
            options=f"-c TimeZone={TZ}"
        )
    return _pool


@contextmanager
def get_connection():
    """Контекст-менеджер для сложных транзакций с ручным управлением курсором."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def query(sql: str, params=(), fetchone: bool = False):
    """SELECT → возвращает кортежи (совместимо с позиционным доступом в main.py)."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if fetchone else cur.fetchall()
    finally:
        pool.putconn(conn)


def query_dict(sql: str, params=(), fetchone: bool = False):
    """SELECT → возвращает dict-строки (для admin_panel.py)."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetchone:
                row = cur.fetchone()
                return dict(row) if row else None
            return [dict(r) for r in cur.fetchall()]
    finally:
        pool.putconn(conn)


def execute(sql: str, params=()):
    """INSERT / UPDATE / DELETE без возврата значений."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def execute_returning(sql: str, params=()):
    """INSERT ... RETURNING id → возвращает вставленный id."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = cur.fetchone()[0]
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def executemany(sql: str, params_list):
    """Пакетный INSERT / UPDATE для списка параметров."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, params_list)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)