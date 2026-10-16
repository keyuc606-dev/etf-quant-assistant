"""Offline execution gates; all wall-clock comparisons use Beijing time."""
import datetime as dt

CN_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
TARGETS = {"morning": dt.time(10, 30), "closing": dt.time(14, 0)}
CUTOFF = dt.time(14, 15)


def execution_allowed(now, session=None):
    if now.tzinfo is None:
        raise ValueError("Execution clock must be timezone aware")
    local = now.astimezone(CN_TZ)
    if local.time() > CUTOFF:
        print(f"execution cutoff exceeded: {local.isoformat()}")
        return False
    if session == "morning" and local.time() > dt.time(11, 30):
        print("Morning session window exceeded; no catch-up")
        return False
    return local.time() >= TARGETS.get(session, dt.time(9, 30))


def require_execution(now, session=None):
    if not execution_allowed(now, session):
        raise RuntimeError("execution cutoff exceeded or session window unavailable")
