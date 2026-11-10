
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo


def calculate_next_run(
    schedule_type,
    timezone="Europe/Istanbul",
    schedule_time=None,
    weekdays=None,
    interval_minutes=None,
    now=None
):
    tz = ZoneInfo(timezone) # Python'a hangi saat diliminde çalışacağımızı söylüyoruz.

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        raise ValueError("now must contain timezone information")
    else:
        now = now.astimezone(tz)

    # 1. Her gün belirli saatte
    if schedule_type == "daily":
        if schedule_time is None:
            raise ValueError("schedule_time is required")

        target_time = time.fromisoformat(schedule_time)

        candidate = datetime.combine(
            now.date(),
            target_time,
            tz
        )

        if candidate <= now:
            candidate += timedelta(days=1)

        return candidate

    # 2. Haftanın belirli günlerinde
    elif schedule_type == "weekly":
        if schedule_time is None:
            raise ValueError("schedule_time is required")

        if not weekdays:
            raise ValueError("weekdays cannot be empty")

        if any(day not in range(7) for day in weekdays):
            raise ValueError("Weekdays must be between 0 and 6")

        target_time = time.fromisoformat(schedule_time)

        for offset in range(8):
            candidate_date = now.date() + timedelta(days=offset)

            if candidate_date.weekday() in weekdays:
                candidate = datetime.combine(
                    candidate_date,
                    target_time,
                    tz
                )

                if candidate > now:
                    return candidate

        raise ValueError("Could not calculate next run")

    # 3. Belirli dakika aralıklarıyla
    elif schedule_type == "interval":
        if interval_minutes is None or interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")

        return now + timedelta(minutes=interval_minutes)

    else:
        raise ValueError("Unsupported schedule type")


if __name__ == "__main__":
    test_now = datetime(
        2026, 11, 9, 15, 0,
        tzinfo=ZoneInfo("Europe/Istanbul")
    )

    next_run = calculate_next_run(
        schedule_type="daily",
        schedule_time="02:00",
        now=test_now
    )

    print("Current time:", test_now.isoformat())
    print("Next backup:", next_run.isoformat())
