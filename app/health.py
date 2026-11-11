"""Can the clock be trusted? Prism decides session phases, closes candles and judges staleness by the PC clock, so a clock that
jumps, a PC that went to sleep, or exchange timestamps that disagree with it must stop BUY decisions until it is sure again.

TimeGuard is called about once a second (the watchdog). It looks for:
  - a stall: far longer than a second between two calls (the PC slept, or the application was suspended or blocked);
  - a clock jump: the wall clock moved by a different amount than the monotonic timer (someone or something set the clock);
  - skew: the spot's exchange timestamps are repeatedly far from the local time (the PC clock is wrong, or the feed is delayed).
When it finds one it reports it, distrusts time, and trusts it again only after a quiet period AND a fresh spot tick that came
after the event. The numbers below are initial assumptions about what is abnormal, not tuned values."""

import time
from collections import deque

STALL_SECONDS = 10.0  # the watchdog runs about every second; this long between two runs is a suspension or a stall
JUMP_SECONDS = 5.0  # wall clock and monotonic timer disagree by more than this, or the wall clock moved back by this much
CALM_SECONDS = 60.0  # time must look normal for this long, with fresh spot ticks, before it is trusted again
MAX_DELAY_SECONDS = 15.0  # a spot tick whose exchange time is this much older than its arrival: PC clock ahead, or a delayed feed
MAX_LEAD_SECONDS = 5.0  # ...or this much newer (the same limit the tick validator uses)
SKEW_STREAK = 3  # consecutive skewed spot ticks before it counts (one late tick after a reconnect is normal)


class TimeGuard:
    def __init__(self, mono=time.monotonic, stall=STALL_SECONDS, jump=JUMP_SECONDS, calm=CALM_SECONDS):
        self._mono, self.stall, self.jump, self.calm = mono, stall, jump, calm
        self._last_wall: float | None = None
        self._last_mono: float | None = None
        self.trusted = True
        self.reason: str | None = None
        self.since: float | None = None
        self.calm_until = 0.0
        self.events: deque = deque(maxlen=20)
        self._skewed = 0

    def _distrust(self, now: float, reason: str, kind: str, start: float | None = None) -> dict:
        self.trusted, self.reason, self.since, self.calm_until = False, reason, now, now + self.calm
        event = {"time": now, "kind": kind, "reason": reason, "from": start}
        self.events.append(event)
        return event

    def check(self, now: float, last_spot_recv: float | None) -> dict | None:
        """Call about once a second. Returns the event when this call found a problem, otherwise None."""
        wall, mono = now, self._mono() if self._mono else None
        prev_wall, prev_mono = self._last_wall, self._last_mono
        self._last_wall, self._last_mono = wall, mono
        event = None
        if prev_wall is not None:
            dw = wall - prev_wall
            dm = None if mono is None or prev_mono is None else mono - prev_mono
            if dw < -self.jump:
                event = self._distrust(now, f"the PC clock moved back by {-dw:.0f} s", "clock_back", prev_wall)
            elif max(dw, dm or 0.0) > self.stall:  # whether the PC slept or the clock was set forward, nothing was seen for this long
                event = self._distrust(now, f"nothing was seen for {max(dw, dm or 0.0):.0f} s: the application was suspended (the PC slept?) or the PC clock was set forward", "suspended", prev_wall)
            elif dm is not None and abs(dw - dm) > self.jump:
                event = self._distrust(now, f"the PC clock jumped by {dw - dm:+.0f} s against the monotonic timer", "clock_jump", prev_wall)
        if not self.trusted and event is None and now >= self.calm_until and last_spot_recv is not None and last_spot_recv > (self.since or 0.0):
            self.trusted, self.reason = True, None
            self.events.append({"time": now, "kind": "trusted_again", "reason": "time looks normal again", "from": None})
        return event

    def note_skew(self, now: float, delta: float) -> dict | None:
        """`delta` = arrival time minus the tick's exchange time, for one spot tick during the session."""
        if -MAX_LEAD_SECONDS <= delta <= MAX_DELAY_SECONDS:
            self._skewed = 0
            return None
        self._skewed += 1
        if self._skewed < SKEW_STREAK or not self.trusted:
            return None
        way = (f"the NIFTY ticks are {delta:.0f} s older than the PC clock says (PC clock ahead, or a delayed feed)" if delta > 0 else
               f"the NIFTY ticks are {-delta:.0f} s ahead of the PC clock (PC clock behind)")  # fmt: skip
        return self._distrust(now, way, "skew")

    def status(self) -> dict:
        return {"trusted": self.trusted, "reason": self.reason, "since": self.since, "events": list(self.events)[-5:]}
