"""Shared data types (plain classes - MicroPython has no dataclasses or enum).

Units used throughout: time = integer local seconds, glucose = integer mg/dL,
insulin = integer milliunits (mU, 1 U = 1000 mU).
"""

from .exact import DecimalError, format_units, is_int, parse_decimal


class SensorStatus:
    OK = "ok"
    NO_DATA = "no_data"
    STALE = "stale"
    FUTURE_TIMESTAMP = "future_timestamp"
    SENSOR_LOW = "sensor_low"  # below the sensor's measurable range ("LO")
    SENSOR_HIGH = "sensor_high"  # above the sensor's measurable range ("HI")
    IMPLAUSIBLE = "implausible"  # physiologically impossible jump
    NO_TREND = "no_trend"  # not enough history and no usable trend arrow


class DoseKind:
    BASAL = "basal"
    AUTO_CORRECTION = "auto_correction"
    BOLUS = "bolus"
    ALL = ("basal", "auto_correction", "bolus")


class Severity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Mode:
    CLOSED_LOOP = "closed_loop"  # CGM trusted: basal adjusted + automatic corrections
    FALLBACK_BASAL = (
        "fallback_basal"  # CGM unreliable, inside the configured fallback window
    )
    NO_CGM_SUSPEND = (
        "no_cgm_suspend"  # CGM unreliable, fallback window over (default: immediately)
    )
    LOW_SUSPEND = "low_suspend"  # low / predicted low
    USER_SUSPEND = "user_suspend"  # suspended by the user
    FAULT = "fault"  # latched hardware/system fault
    DELIVERING = ("closed_loop", "fallback_basal")


class GlucoseReading:
    __slots__ = ("timestamp", "mg_dl", "trend")

    def __init__(self, timestamp, mg_dl, trend=None):
        self.timestamp = timestamp  # int seconds
        self.mg_dl = mg_dl  # int mg/dL
        self.trend = trend  # trend arrow name reported by the CGM, or None

    def __repr__(self):
        return "GlucoseReading(%r, %r, %r)" % (self.timestamp, self.mg_dl, self.trend)


class Dose:
    __slots__ = ("timestamp", "mu", "kind")

    def __init__(self, timestamp, mu, kind=DoseKind.BASAL):
        self.timestamp = timestamp
        self.mu = mu
        self.kind = kind

    def __repr__(self):
        return "Dose(%r, %r, %r)" % (self.timestamp, self.mu, self.kind)


class DeliveryResult:
    __slots__ = ("success", "delivered_mu", "message")

    def __init__(self, success, delivered_mu, message=""):
        self.success = success
        self.delivered_mu = delivered_mu
        self.message = message


class DeviceInfoError(ValueError):
    pass


def _positive_int(info, key, errors, allow_zero=False):
    value = info.get(key) if isinstance(info, dict) else None
    if not is_int(value) or value < 0 or (value == 0 and not allow_zero):
        errors.append(
            "%s must be a %s integer, got %r"
            % (key, "non-negative" if allow_zero else "positive", value)
        )
        return 0
    return value


class PumpInfo:
    """Hardware facts reported by the pump driver (all insulin in mU)."""

    KEYS = (
        "increment_mu",
        "max_single_delivery_mu",
        "max_hourly_mu",
        "max_daily_mu",
        "reservoir_capacity_mu",
    )

    def __init__(self, info):
        errors = []
        for key in self.KEYS:
            setattr(self, key, _positive_int(info, key, errors))
        if not errors:
            if self.max_single_delivery_mu % self.increment_mu:
                errors.append(
                    "max_single_delivery_mu must be a multiple of increment_mu"
                )
            if (
                self.max_single_delivery_mu > self.max_hourly_mu
                or self.max_hourly_mu > self.max_daily_mu
            ):
                errors.append("pump limits must satisfy single <= hourly <= daily")
        if errors:
            raise DeviceInfoError("invalid pump info: " + "; ".join(errors))


class SensorInfo:
    """Facts reported by the CGM: measurable range and trend arrow meanings.

    `max_rate` (exact, mg/dL/min) is the profile's plausibility limit; an arrow claiming a
    faster change than that is treated as corrupt device data."""

    def __init__(self, info, arrows, max_rate):
        errors = []
        self.min_mg_dl = _positive_int(info, "min_mg_dl", errors)
        self.max_mg_dl = _positive_int(info, "max_mg_dl", errors)
        if not errors and self.min_mg_dl >= self.max_mg_dl:
            errors.append("min_mg_dl must be below max_mg_dl")
        self.arrow_rates = {}
        if not isinstance(arrows, dict) or not arrows:
            errors.append("trend arrow table must be a non-empty object")
        else:
            for name in sorted(arrows):
                try:
                    rate = parse_decimal(arrows[name])
                except DecimalError as exc:
                    errors.append("trend arrow %r rate %s" % (name, exc))
                    continue
                if not isinstance(name, str) or abs(rate) > max_rate:
                    errors.append("trend arrow %r has an implausible rate" % (name,))
                    continue
                self.arrow_rates[name] = rate
        if errors:
            raise DeviceInfoError("invalid CGM info: " + "; ".join(errors))


class Alert:
    __slots__ = ("severity", "code", "message")

    def __init__(self, severity, code, message):
        self.severity = severity
        self.code = code
        self.message = message


class CycleDecision:
    def __init__(
        self,
        timestamp,
        sensor_status,
        glucose,
        trend,
        predicted,
        iob_mu,
        scheduled_basal,
    ):
        self.timestamp = timestamp
        self.mode = Mode.CLOSED_LOOP
        self.sensor_status = sensor_status
        self.glucose = glucose  # int mg/dL or None
        self.trend = trend  # arrow name or None
        self.predicted_glucose = predicted  # int mg/dL (rounded down) or None
        self.iob_mu = iob_mu
        self.scheduled_basal = scheduled_basal  # exact Q milliunits for this cycle
        self.basal_request = 0  # Q or int milliunits
        self.correction_mu = 0
        self.delivered_mu = 0
        self.alerts = []

    def alert(self, severity, code, message):
        self.alerts.append(Alert(severity, code, message))

    def alert_codes(self):
        return set(a.code for a in self.alerts)


class BolusRecommendation:
    def __init__(
        self,
        id,
        created_at,
        carbs_g,
        glucose,
        glucose_source,
        carb_ratio,
        correction_factor,
        target,
        carb_mu,
        correction_mu,
        iob_mu,
        recommended_mu,
        warnings,
        blocked_reason,
    ):
        self.id = id
        self.created_at = created_at
        self.carbs_g = carbs_g
        self.glucose = glucose
        self.glucose_source = glucose_source  # "cgm", "meter" or "none"
        self.carb_ratio = carb_ratio
        self.correction_factor = correction_factor
        self.target = target
        self.carb_mu = carb_mu
        self.correction_mu = correction_mu
        self.iob_mu = iob_mu
        self.recommended_mu = recommended_mu
        self.warnings = tuple(warnings)
        self.blocked_reason = blocked_reason

    @property
    def blocked(self):
        return self.blocked_reason is not None

    def summary(self):
        if self.blocked:
            return "BLOCKED: %s" % self.blocked_reason
        return "%s U (carbs %s U + correction %s U, IOB %s U)" % (
            format_units(self.recommended_mu),
            format_units(self.carb_mu),
            format_units(self.correction_mu),
            format_units(self.iob_mu),
        )


class BolusResult:
    __slots__ = ("success", "delivered_mu", "message")

    def __init__(self, success, delivered_mu, message):
        self.success = success
        self.delivered_mu = delivered_mu
        self.message = message


class AuditEvent:
    __slots__ = ("timestamp", "event", "details")

    def __init__(self, timestamp, event, details):
        self.timestamp = timestamp
        self.event = event
        self.details = details
