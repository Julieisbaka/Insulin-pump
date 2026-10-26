"""Time-of-day schedules for therapy settings.

A schedule has an optional all-day ``default`` plus any number of time-of-day
``segments`` that override it, e.g. a carb ratio of 1:9 only from 06:00-11:00.
Segments may wrap past midnight (22:00-06:00). Segments must not overlap, and
without a default they must cover the full 24 hours, so every minute of the day
always has exactly one unambiguous value.

Values are stored already converted to their exact type (int milliunits, int
mg/dL or exact rationals); this module only deals with *when* they apply.
"""

MINUTES_PER_DAY = 1440
SECONDS_PER_DAY = MINUTES_PER_DAY * 60


class ScheduleError(ValueError):
    pass


def parse_time_of_day(text, is_end=False):
    """'HH:MM' -> minutes after midnight. As an end time, '00:00' and '24:00' mean midnight (1440)."""
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        raise ScheduleError("time must be 'HH:MM', got %r" % (text,))
    hours, minutes = text[:2], text[3:]
    if not hours.isdigit() or not minutes.isdigit():
        raise ScheduleError("time must be 'HH:MM', got %r" % (text,))
    h, m = int(hours), int(minutes)
    if is_end and h == 24 and m == 0:
        return MINUTES_PER_DAY
    if h > 23 or m > 59:
        raise ScheduleError("invalid time %r" % (text,))
    minute = h * 60 + m
    if is_end and minute == 0:
        return MINUTES_PER_DAY
    return minute


def format_time_of_day(minute):
    return "%02d:%02d" % (minute // 60, minute % 60)


def minute_of_day(timestamp_s):
    """Local minute of day for an integer local timestamp in seconds."""
    return (timestamp_s // 60) % MINUTES_PER_DAY


def format_clock(timestamp_s):
    return format_time_of_day(minute_of_day(timestamp_s))


class Segment:
    __slots__ = ("start", "end", "value")

    def __init__(self, start, end, value):
        if type(start) is not int or not 0 <= start <= 1439:
            raise ScheduleError(
                "segment start must be a minute of day 0-1439, got %r" % (start,)
            )
        if type(end) is not int or not 1 <= end <= 1440:
            raise ScheduleError(
                "segment end must be a minute of day 1-1440, got %r" % (end,)
            )
        self.start = start
        self.end = end
        self.value = value
        if start == end % MINUTES_PER_DAY and not (
            start == 0 and end == MINUTES_PER_DAY
        ):
            raise ScheduleError(
                "segment %s has zero length; use 00:00-24:00 for all day" % self.label
            )

    @property
    def label(self):
        return "%s-%s" % (format_time_of_day(self.start), format_time_of_day(self.end))

    def intervals(self):
        if self.start < self.end:
            return ((self.start, self.end),)
        return ((self.start, MINUTES_PER_DAY), (0, self.end))

    def contains(self, minute):
        for a, b in self.intervals():
            if a <= minute < b:
                return True
        return False


class Schedule:
    def __init__(self, name, segments=(), default=None):
        self.name = name
        self.segments = tuple(sorted(segments, key=lambda s: s.start))
        self.default = default
        if not self.segments and default is None:
            raise ScheduleError(
                "%s: needs a default value or at least one segment" % name
            )

        pieces = []
        for seg in self.segments:
            for a, b in seg.intervals():
                pieces.append((a, b, seg))
        pieces.sort(key=lambda p: (p[0], p[1]))
        for first, second in zip(pieces, pieces[1:]):
            if second[0] < first[1]:
                raise ScheduleError(
                    "%s: segments %s and %s overlap"
                    % (name, first[2].label, second[2].label)
                )

        if default is None:
            gaps, cursor = [], 0
            for a, b, _ in pieces:
                if a > cursor:
                    gaps.append(
                        "%s-%s" % (format_time_of_day(cursor), format_time_of_day(a))
                    )
                cursor = max(cursor, b)
            if cursor < MINUTES_PER_DAY:
                gaps.append("%s-24:00" % format_time_of_day(cursor))
            if gaps:
                raise ScheduleError(
                    "%s: no default value and no segment covers %s"
                    % (name, ", ".join(gaps))
                )

    @classmethod
    def constant(cls, name, value):
        return cls(name, (), value)

    def value_at_minute(self, minute):
        for segment in self.segments:
            if segment.contains(minute):
                return segment.value
        return self.default

    def value_at(self, timestamp_s):
        return self.value_at_minute(minute_of_day(timestamp_s))

    def values(self):
        values = [s.value for s in self.segments]
        if self.default is not None:
            values.append(self.default)
        return values

    def change_points(self):
        points = {0}
        for segment in self.segments:
            points.add(segment.start)
            points.add(segment.end % MINUTES_PER_DAY)
        return points

    def describe(self, fmt=str):
        parts = ["default %s" % fmt(self.default)] if self.default is not None else []
        parts += ["%s = %s" % (s.label, fmt(s.value)) for s in self.segments]
        return "%s: %s" % (self.name, ", ".join(parts))

    @classmethod
    def from_config(cls, name, raw, convert):
        """Build from validated JSON ({"default": x, "segments": [...]}); `convert` maps raw numbers to exact values."""
        segments = []
        for item in raw.get("segments", []):
            segments.append(
                Segment(
                    parse_time_of_day(item["start"]),
                    parse_time_of_day(item["end"], is_end=True),
                    convert(item["value"]),
                )
            )
        default = raw.get("default")
        return cls(name, segments, None if default is None else convert(default))
