"""CGM data validation and short-term glucose prediction (exact integer/rational maths).

The controller never uses a raw reading directly; it uses a GlucoseAssessment,
which classifies the data so that unreliable values cannot drive dosing. All
thresholds come from the profile's ``cgm`` settings, and the sensor range and
the meaning of each trend arrow come from the CGM itself (SensorInfo).
"""

from .exact import Q, is_int, q_min
from .models import GlucoseReading, SensorStatus


class GlucoseAssessment:
    def __init__(
        self,
        status,
        reading,
        rate=None,
        predicted=None,
        message="",
        slope=None,
        arrow_rate=None,
    ):
        self.status = status
        self.reading = reading
        self.rate = rate  # Q, mg/dL per minute (combined estimate)
        self.predicted = predicted  # int mg/dL at the prediction horizon, rounded DOWN
        self.message = message
        self.slope = slope  # Q from the least-squares fit, or None
        self.arrow_rate = arrow_rate  # Q from the CGM trend arrow, or None

    @property
    def reliable(self):
        return self.status == SensorStatus.OK

    @property
    def glucose(self):
        return self.reading.mg_dl if self.reading is not None else None

    def is_low(self, threshold):
        """Current glucose is at/below threshold. Errs toward 'low': out-of-range, implausible,
        stale and future-dated readings still count if their value is low."""
        if self.status == SensorStatus.SENSOR_LOW:
            return True
        if self.reading is None:
            return False
        return self.reading.mg_dl <= threshold

    def indicates_low(self, threshold):
        """Current or predicted glucose is at/below threshold."""
        if self.is_low(threshold):
            return True
        return (
            self.reliable and self.predicted is not None and self.predicted <= threshold
        )


def _valid_reading(item):
    return (
        isinstance(item, GlucoseReading)
        and is_int(item.timestamp)
        and is_int(item.mg_dl)
    )


def assess_glucose(reading, history, trend, now, cgm, sensor):
    """Classify CGM data. `cgm` is the profile's cgm settings; `sensor` is the CGM's SensorInfo (or None)."""
    if not _valid_reading(reading):
        return GlucoseAssessment(
            SensorStatus.NO_DATA, None, message="No valid CGM reading available"
        )
    age_s = now - reading.timestamp
    if age_s < -cgm.future_tolerance_minutes * 60:
        return GlucoseAssessment(
            SensorStatus.FUTURE_TIMESTAMP,
            reading,
            message="CGM reading is timestamped in the future",
        )
    if age_s > cgm.max_reading_age_minutes * 60:
        return GlucoseAssessment(
            SensorStatus.STALE,
            reading,
            message="Last CGM reading is %d min old" % (age_s // 60),
        )
    if sensor is None:
        return GlucoseAssessment(
            SensorStatus.NO_DATA,
            reading,
            message="CGM device information (range / trend arrows) unavailable",
        )
    if reading.mg_dl < sensor.min_mg_dl:
        return GlucoseAssessment(
            SensorStatus.SENSOR_LOW,
            reading,
            message="CGM reads below its measurable range (LO)",
        )
    if reading.mg_dl > sensor.max_mg_dl:
        return GlucoseAssessment(
            SensorStatus.SENSOR_HIGH,
            reading,
            message="CGM reads above its measurable range (HI)",
        )

    points = _clean_history(reading, history, cgm, sensor)
    if len(points) >= 2:
        prev = points[-2]
        gap_s = reading.timestamp - prev.timestamp
        if (
            gap_s > 0
            and Q(abs(reading.mg_dl - prev.mg_dl) * 60, gap_s) > cgm.max_plausible_rate
        ):
            return GlucoseAssessment(
                SensorStatus.IMPLAUSIBLE,
                reading,
                message="Implausible CGM jump from %d to %d mg/dL in %d s"
                % (prev.mg_dl, reading.mg_dl, gap_s),
            )

    slope = _least_squares_slope(points, cgm)
    arrow = sensor.arrow_rates.get(trend) if isinstance(trend, str) else None
    if slope is not None and arrow is not None:
        rate = (
            q_min(slope, arrow)
            if cgm.rate_combination == "lower"
            else (slope + arrow) / 2
        )
    elif slope is not None:
        rate = slope
    elif arrow is not None:
        rate = arrow
    else:
        return GlucoseAssessment(
            SensorStatus.NO_TREND,
            reading,
            message="Not enough CGM history and no recognised trend arrow to estimate the trend",
        )
    predicted = (rate * cgm.prediction_horizon_minutes + reading.mg_dl).floor()
    return GlucoseAssessment(
        SensorStatus.OK, reading, rate, predicted, slope=slope, arrow_rate=arrow
    )


def _clean_history(reading, history, cgm, sensor):
    cutoff = reading.timestamp - cgm.history_window_minutes * 60
    by_time = {}
    for item in history or []:
        if (
            _valid_reading(item)
            and sensor.min_mg_dl <= item.mg_dl <= sensor.max_mg_dl
            and cutoff <= item.timestamp <= reading.timestamp
        ):
            by_time[item.timestamp] = item
    by_time[reading.timestamp] = reading
    return [by_time[t] for t in sorted(by_time)]


def _least_squares_slope(points, cgm):
    """Exact least-squares slope (mg/dL per minute) over the rate window, or None if data is insufficient."""
    latest = points[-1].timestamp
    recent = [p for p in points if p.timestamp >= latest - cgm.rate_window_minutes * 60]
    if len(recent) < cgm.min_rate_points:
        return None
    t0 = recent[0].timestamp
    xs = [p.timestamp - t0 for p in recent]
    if xs[-1] - xs[0] < cgm.min_rate_span_minutes * 60:
        return None
    ys = [p.mg_dl for p in recent]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = n * sum(x * x for x in xs) - sx * sx
    sxy = n * sum(x * y for x, y in zip(xs, ys)) - sx * sy
    if sxx == 0:
        return None
    return Q(sxy * 60, sxx)
