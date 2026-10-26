"""Clocks returning integer local seconds (MicroPython has no datetime)."""

import time


class SimulatedClock:
    def __init__(self, start_s=0):
        self._now = start_s

    def now(self):
        return self._now

    def advance(self, minutes=0, seconds=0):
        self._now += minutes * 60 + seconds

    def set(self, timestamp_s):
        self._now = timestamp_s


class SystemClock:
    """Real-time clock: integer seconds from time.time() shifted to local time.

    Schedules use local time of day, so `utc_offset_minutes` must match the user's
    time zone (on a Pico, set the RTC first, e.g. with ntptime on a Pico W).
    """

    def __init__(self, utc_offset_minutes=0):
        self._offset_s = utc_offset_minutes * 60

    def now(self):
        return int(time.time()) + self._offset_s
