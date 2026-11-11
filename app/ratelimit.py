"""One shared allowance for Angel One's candle API (`getCandleData`).

The history refresh and the gap recovery both call it, from different threads, and the API refuses bursts with
"Access denied because of exceeding access rate". This spaces every request (the API allows about 3 a second), and after
a refusal makes every caller wait out a growing cool-down instead of hammering it. It only ever delays a request: it never
drops one, never invents an answer, and records what it saw so the dashboard can show it."""

import threading
import time

MIN_GAP = 0.4  # seconds between two candle requests: about 3 a second
COOLDOWNS = (5.0, 15.0, 30.0, 60.0)  # after the 1st, 2nd, 3rd, 4th+ refusal in a row
REFUSAL = "exceeding access rate"


def is_refusal(error) -> bool:
    return REFUSAL in str(error).lower()


class CandleLimiter:
    def __init__(self, clock=time.time, sleep=time.sleep):
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next_ok = 0.0
        self.cooldown_until = 0.0
        self.consecutive = 0
        self.requests = self.refusals = 0
        self.last_refusal: float | None = None
        self.last_error: str | None = None

    def wait(self) -> float:
        """Block until this request may go out; returns the seconds waited. Slots are handed out in order, so two threads never
        fire together."""
        with self._lock:
            start = max(self._clock(), self._next_ok, self.cooldown_until)
            self._next_ok = start + MIN_GAP
            self.requests += 1
        delay = start - self._clock()
        if delay > 0:
            self._sleep(delay)
        return max(delay, 0.0)

    def ok(self) -> None:
        with self._lock:
            self.consecutive = 0

    def refused(self, error) -> None:
        """The API refused a request for its rate: everything waits for a cool-down that grows while the refusals go on."""
        with self._lock:
            self.refusals += 1
            self.consecutive += 1
            now = self._clock()
            self.cooldown_until = max(self.cooldown_until, now + COOLDOWNS[min(self.consecutive, len(COOLDOWNS)) - 1])
            self.last_refusal, self.last_error = now, str(error)[:160]

    def status(self) -> dict:
        now = self._clock()
        return {"api_requests": self.requests, "refusals": self.refusals, "last_refusal": self.last_refusal,
                "cooling_down": self.cooldown_until > now, "cooldown_seconds": max(0.0, self.cooldown_until - now), "refusal_error": self.last_error}  # fmt: skip
