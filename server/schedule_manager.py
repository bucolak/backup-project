
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database import get_connection
from scheduler import calculate_next_run


def create_schedule(
    client_id,
    schedule_type,
    schedule_time=None,
    weekdays=None,
    interval_minutes=None,
    timezone="Europe/Istanbul"
):
    # Şu anki zamanı belirle
    now = datetime.now(ZoneInfo(timezone))

    # Bir sonraki yedekleme zamanını hesapla
    next_run = calculate_next_run(
        schedule_type=schedule_type,
        timezone=timezone,
        schedule_time=schedule_time,
        weekdays=weekdays,
        interval_minutes=interval_minutes,
        now=now
    )

    # Haftanın günlerini veritabanında JSON metni olarak sakla
    weekdays_json = (
        json.dumps(weekdays) if weekdays is not None else None
    )

    with get_connection() as connection:
        cursor = connection.execute("""
            INSERT INTO backup_schedules (
                client_id,
                schedule_type,
                schedule_time,
                weekdays,
                interval_minutes,
                timezone,
                next_run_at,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client_id,
            schedule_type,
            schedule_time,
            weekdays_json,
            interval_minutes,
            timezone,
            next_run.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds")
        ))

        schedule_id = cursor.lastrowid

    return {
        "id": schedule_id,
        "client_id": client_id,
        "schedule_type": schedule_type,
        "next_run_at": next_run.isoformat(timespec="seconds")
    }


def process_due_schedules(now=None):
    # Kontrol edilecek zamanı belirle
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("now must contain timezone information")
    else:
        now = now.astimezone(timezone.utc)

    created_jobs = 0

    with get_connection() as connection:

        # Aktif zamanlamaları getir
        schedules = connection.execute("""
            SELECT *
            FROM backup_schedules
            WHERE enabled = 1
        """).fetchall()

        for schedule in schedules:

            next_run = datetime.fromisoformat(
                schedule["next_run_at"]
            )

            # Zamanı gelmiş bütün yedeklemeleri işle
            while next_run.astimezone(timezone.utc) <= now:

                weekdays = (
                    json.loads(schedule["weekdays"])
                    if schedule["weekdays"] is not None
                    else None
                )

                # Bekleyen yedekleme görevini oluştur
                cursor = connection.execute("""
                    INSERT OR IGNORE INTO backup_jobs (
                        schedule_id,
                        client_id,
                        scheduled_for,
                        status,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    schedule["id"],
                    schedule["client_id"],
                    next_run.isoformat(timespec="seconds"),
                    "pending",
                    datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                ))

                if cursor.rowcount == 1:
                    created_jobs += 1

                # Bir sonraki çalışma zamanını hesapla
                next_run = calculate_next_run(
                    schedule_type=schedule["schedule_type"],
                    timezone=schedule["timezone"],
                    schedule_time=schedule["schedule_time"],
                    weekdays=weekdays,
                    interval_minutes=schedule["interval_minutes"],
                    now=next_run
                )

            # Güncellenen zamanı veritabanına kaydet
            connection.execute("""
                UPDATE backup_schedules
                SET next_run_at = ?
                WHERE id = ?
            """, (
                next_run.isoformat(timespec="seconds"),
                schedule["id"]
            ))

    return created_jobs
