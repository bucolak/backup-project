
import sqlite3
import os
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "backup.db")


@contextmanager
def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    with get_connection() as connection:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS backup_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_time TEXT,
                weekdays TEXT,
                interval_minutes INTEGER,
                timezone TEXT NOT NULL DEFAULT 'Europe/Istanbul',
                next_run_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,

                CHECK (
                    schedule_type IN ('daily', 'weekly', 'interval')
                )
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS backup_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,

                FOREIGN KEY (schedule_id)
                    REFERENCES backup_schedules(id),

                UNIQUE (schedule_id, scheduled_for),

                CHECK (
                    status IN (
                        'pending',
                        'running',
                        'success',
                        'failed'
                    )
                )
            )
        """)


if __name__ == "__main__":
    initialize_database()
    print("Database initialized successfully.")
