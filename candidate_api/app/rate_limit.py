import hashlib
import hmac
from datetime import timedelta

from candidate_api.app.db import connect_database


class PostgresRateLimiter:
    def __init__(self, database_url: str | None, key_secret: str) -> None:
        self.database_url = database_url
        self.key_secret = key_secret.encode("utf-8")

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        if limit < 1 or limit > 1000 or window_seconds < 1:
            raise ValueError("Rate limits must be between 1 and 1000 and windows must be positive.")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for shared rate limiting.")
        key_hash = hmac.new(self.key_secret, key.encode("utf-8"), hashlib.sha256).digest()
        lock_key = int.from_bytes(key_hash[:8], "big", signed=True)
        with connect_database(self.database_url) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                now = connection.execute("SELECT statement_timestamp() AS now").fetchone()["now"]
                row = connection.execute(
                    "SELECT window_seconds, event_times FROM rate_limit_buckets WHERE key_hash = %s FOR UPDATE",
                    (key_hash,),
                ).fetchone()
                recent = (
                    [event_at for event_at in row["event_times"] if event_at > now - timedelta(seconds=window_seconds)]
                    if row and row["window_seconds"] == window_seconds
                    else []
                )
                allowed = len(recent) < limit
                if allowed:
                    recent.append(now)
                expires_at = (
                    recent[-1] + timedelta(seconds=window_seconds)
                    if recent else now + timedelta(seconds=window_seconds)
                )
                connection.execute(
                    """INSERT INTO rate_limit_buckets (key_hash, window_seconds, event_times, expires_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (key_hash) DO UPDATE SET
                           window_seconds = EXCLUDED.window_seconds,
                           event_times = EXCLUDED.event_times,
                           expires_at = EXCLUDED.expires_at""",
                    (key_hash, window_seconds, recent, expires_at),
                )
                cleanup = connection.execute(
                    "SELECT pg_try_advisory_xact_lock(871223892211) AS acquired"
                ).fetchone()["acquired"]
                if cleanup:
                    connection.execute(
                        """WITH expired AS (
                               SELECT key_hash FROM rate_limit_buckets
                               WHERE expires_at <= %s ORDER BY expires_at LIMIT 500 FOR UPDATE SKIP LOCKED
                           )
                           DELETE FROM rate_limit_buckets AS bucket USING expired
                           WHERE bucket.key_hash = expired.key_hash""",
                        (now,),
                    )
        return allowed
