"""Small web helpers shared by routers (kept out of main.py to avoid import cycles)."""

UID_COOKIE = "sched_uid"


def browser_uid(cookies) -> str | None:
    """The per-browser id set on the first visit; None if the client has no cookie yet."""
    return cookies.get(UID_COOKIE)
