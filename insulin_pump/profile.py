"""Therapy profile: per-person, time-of-day settings plus safety limits.

The JSON schema (``schemas/profile.schema.json``) is the single source of truth
for structure, hard limits and default values. Loading a profile:

1. fills in schema defaults for omitted optional settings,
2. validates against the schema (types, ranges, decimal places, unknown keys),
3. converts every number to an exact type (insulin -> int milliunits,
   ratios -> exact rationals),
4. runs the cross-field checks the schema cannot express, at every schedule
   change point of the day (urgent low < low < target <= correct-above < high
   alert; basal <= max basal; coherent limits).

Any problem rejects the whole profile, and every error is listed.
"""

import json

from .exact import (
    MU_PER_UNIT,
    Q,
    format_decimal,
    format_units,
    parse_decimal,
    to_scaled_int,
)
from .insulin_curve import CurveError, InsulinCurve
from .schedule import (
    MINUTES_PER_DAY,
    Schedule,
    ScheduleError,
    format_time_of_day,
    minute_of_day,
)
from .schema import Validator

SCHEDULE_NAMES = (
    "basal_rate",
    "carb_ratio",
    "correction_factor",
    "target",
    "correct_above",
    "low_threshold",
)
SECTION_NAMES = (
    "insulin",
    "limits",
    "alarms",
    "automation",
    "cgm",
    "low_suspend",
    "bolus",
    "loop",
    "logging",
)
SCHEMA_RELATIVE_PATH = "schemas/profile.schema.json"

_validator_cache = {}


class ProfileValidationError(ValueError):
    def __init__(self, errors):
        unique = []
        for error in errors:
            if error not in unique:
                unique.append(error)
        self.errors = unique
        super().__init__("Invalid therapy profile:\n  - " + "\n  - ".join(unique))


def _package_dir():
    try:
        path = __file__
    except NameError:
        return "insulin_pump"
    cut = max(path.rfind("/"), path.rfind("\\"))
    return path[:cut] if cut >= 0 else "."


def get_validator(schema_path=None):
    path = schema_path or (_package_dir() + "/" + SCHEMA_RELATIVE_PATH)
    validator = _validator_cache.get(path)
    if validator is None:
        with open(path, encoding="utf-8") as fh:
            validator = Validator(json.load(fh))
        _validator_cache[path] = validator
    return validator


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _converter(node):
    """Return (converter, renamer) for a resolved schema node, based on its x-type."""
    kind = node.get("x-type")
    if kind == "insulin":
        return (lambda v: to_scaled_int(v, MU_PER_UNIT, 3)), _insulin_name
    if kind == "rational":
        return (lambda v: parse_decimal(v, node.get("x-decimals"))), None
    return (lambda v: v), None


def _insulin_name(key):
    if key.endswith("_units_per_hour"):
        return key[: -len("_units_per_hour")] + "_mu_per_hour"
    if key.endswith("_units"):
        return key[: -len("_units")] + "_mu"
    return key + "_mu"


class Section:
    """Settings group; attribute names of insulin values end in _mu (integer milliunits)."""

    def __init__(self, name, values):
        self._name = name
        self._values = values

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        values = self._values
        if key in values:
            return values[key]
        raise AttributeError("%s has no setting %r" % (self._name, key))

    def items(self):
        return [(k, self._values[k]) for k in sorted(self._values)]

    def describe(self):
        parts = []
        for key, value in self.items():
            if key.endswith("_mu") or key.endswith("_mu_per_hour"):
                parts.append("%s=%s U" % (key, format_units(value, 3)))
            elif isinstance(value, Q):
                parts.append("%s=%s" % (key, format_decimal(value)))
            else:
                parts.append("%s=%s" % (key, value))
        return "%s: %s" % (self._name, ", ".join(parts))


class ProfileSnapshot:
    """All scheduled values in effect at one moment (insulin as mU/hr, ratios as Q, glucose as int mg/dL)."""

    __slots__ = SCHEDULE_NAMES

    def __init__(self, values):
        for name in SCHEDULE_NAMES:
            setattr(self, name, values[name])

    def key(self):
        return tuple(getattr(self, name) for name in SCHEDULE_NAMES)

    def __eq__(self, other):
        return isinstance(other, ProfileSnapshot) and self.key() == other.key()

    def __ne__(self, other):
        return not self.__eq__(other)


class TherapyProfile:
    def __init__(self, data, schema_path=None):
        if not isinstance(data, dict):
            raise ProfileValidationError(["profile must be a JSON object"])
        validator = get_validator(schema_path)
        raw = validator.apply_defaults(_deep_copy(data))
        errors = validator.validate(raw)
        if errors:
            raise ProfileValidationError(errors)

        self.raw = raw
        self.name = raw["name"]
        root = validator.resolve(validator.root)
        props = root["properties"]
        for name in SCHEDULE_NAMES:
            schedule_node = validator.resolve(props[name])
            value_node = validator.resolve(schedule_node["properties"]["default"])
            convert = _converter(value_node)[0]
            try:
                setattr(self, name, Schedule.from_config(name, raw[name], convert))
            except ScheduleError as exc:
                errors.append(str(exc))
        for section in SECTION_NAMES:
            section_node = validator.resolve(props[section])
            values = {}
            for key, value in raw[section].items():
                convert, rename = _converter(
                    validator.resolve(section_node["properties"][key])
                )
                values[rename(key) if rename else key] = convert(value)
            setattr(self, section, Section(section, values))
        if errors:
            raise ProfileValidationError(errors)

        try:
            self.curve = InsulinCurve(
                self.insulin.curve,
                self.insulin.duration_minutes,
                self.insulin.peak_minutes,
                self.insulin.delay_minutes,
            )
        except CurveError as exc:
            errors.append("insulin: %s" % exc)
        errors.extend(self._cross_checks())
        if errors:
            raise ProfileValidationError(errors)

    # ---- queries ---------------------------------------------------------------------

    def at(self, timestamp_s):
        return self.at_minute(minute_of_day(timestamp_s))

    def at_minute(self, minute):
        return ProfileSnapshot(
            {
                name: getattr(self, name).value_at_minute(minute)
                for name in SCHEDULE_NAMES
            }
        )

    def change_points(self):
        points = set()
        for name in SCHEDULE_NAMES:
            points |= getattr(self, name).change_points()
        return sorted(points)

    def daily_basal_mu(self):
        """Exact scheduled basal over 24 h (Q, milliunits)."""
        total = Q(0)
        points = self.basal_rate.change_points()
        points = sorted(points) + [MINUTES_PER_DAY]
        for start, end in zip(points, points[1:]):
            total = total + Q(
                self.basal_rate.value_at_minute(start) * (end - start), 60
            )
        return total

    def describe(self):
        lines = ["Profile '%s'" % self.name]
        lines.append(
            "  " + self.basal_rate.describe(lambda v: format_units(v, 3) + " U/hr")
        )
        lines.append(
            "  " + self.carb_ratio.describe(lambda v: format_decimal(v) + " g/U")
        )
        lines.append(
            "  "
            + self.correction_factor.describe(lambda v: format_decimal(v) + " mg/dL/U")
        )
        for name in ("target", "correct_above", "low_threshold"):
            lines.append("  " + getattr(self, name).describe(lambda v: "%d mg/dL" % v))
        for section in SECTION_NAMES:
            lines.append("  " + getattr(self, section).describe())
        return "\n".join(lines)

    # ---- validation ------------------------------------------------------------------

    def _cross_checks(self):
        errors = []
        limits, alarms, cgm = self.limits, self.alarms, self.cgm
        # Values are constant between change points, so checking each one covers the whole day.
        for minute in self.change_points():
            s = self.at_minute(minute)
            at = "At %s" % format_time_of_day(minute)
            if not s.low_threshold < s.target:
                errors.append(
                    "%s: low threshold (%d) must be below target (%d)"
                    % (at, s.low_threshold, s.target)
                )
            if not s.target <= s.correct_above:
                errors.append(
                    "%s: target (%d) must not be above correct-above (%d)"
                    % (at, s.target, s.correct_above)
                )
            if not alarms.urgent_low < s.low_threshold:
                errors.append(
                    "%s: urgent low alarm (%d) must be below low threshold (%d)"
                    % (at, alarms.urgent_low, s.low_threshold)
                )
            if not s.correct_above < alarms.high_alert:
                errors.append(
                    "%s: correct-above (%d) must be below high alert (%d)"
                    % (at, s.correct_above, alarms.high_alert)
                )
            if s.basal_rate > limits.max_basal_rate_mu_per_hour:
                errors.append(
                    "%s: basal rate %s U/hr exceeds max basal rate %s U/hr"
                    % (
                        at,
                        format_units(s.basal_rate, 3),
                        format_units(limits.max_basal_rate_mu_per_hour, 3),
                    )
                )

        if max(self.basal_rate.values()) > limits.max_hourly_mu:
            errors.append(
                "limits.max_hourly_units is lower than the highest scheduled basal rate"
            )
        daily_basal = self.daily_basal_mu()
        if Q(limits.max_daily_mu) < daily_basal + limits.max_bolus_mu:
            errors.append(
                "limits.max_daily_units (%s U) must allow the daily scheduled basal (%s U) plus one max bolus (%s U)"
                % (
                    format_units(limits.max_daily_mu),
                    format_decimal(daily_basal / MU_PER_UNIT, 3),
                    format_units(limits.max_bolus_mu),
                )
            )
        if limits.max_bolus_mu > limits.max_hourly_mu:
            errors.append(
                "limits.max_bolus_units cannot exceed limits.max_hourly_units"
            )
        if limits.max_bolus_mu > limits.max_iob_mu:
            errors.append("limits.max_bolus_units cannot exceed limits.max_iob_units")
        if self.automation.max_correction_per_cycle_mu > limits.max_bolus_mu:
            errors.append(
                "automation.max_correction_per_cycle_units cannot exceed limits.max_bolus_units"
            )
        if alarms.critical_reservoir_mu > alarms.low_reservoir_mu:
            errors.append(
                "alarms.critical_reservoir_units cannot exceed alarms.low_reservoir_units"
            )
        if cgm.rate_window_minutes > cgm.history_window_minutes:
            errors.append(
                "cgm.rate_window_minutes cannot exceed cgm.history_window_minutes"
            )
        if cgm.min_rate_span_minutes > cgm.rate_window_minutes:
            errors.append(
                "cgm.min_rate_span_minutes cannot exceed cgm.rate_window_minutes"
            )
        if self.bolus.meter_min_mg_dl >= self.bolus.meter_max_mg_dl:
            errors.append("bolus.meter_min_mg_dl must be below bolus.meter_max_mg_dl")
        return errors

    def device_errors(self, device):
        """Problems combining this profile with a specific pump (see PumpInfo)."""
        errors = []
        limits = self.limits
        if limits.max_bolus_mu > device.max_single_delivery_mu:
            errors.append(
                "limits.max_bolus_units (%s U) exceeds the pump's max single delivery (%s U)"
                % (
                    format_units(limits.max_bolus_mu),
                    format_units(device.max_single_delivery_mu),
                )
            )
        if limits.max_hourly_mu > device.max_hourly_mu:
            errors.append(
                "limits.max_hourly_units exceeds the pump's hourly limit (%s U)"
                % format_units(device.max_hourly_mu)
            )
        if limits.max_daily_mu > device.max_daily_mu:
            errors.append(
                "limits.max_daily_units exceeds the pump's daily limit (%s U)"
                % format_units(device.max_daily_mu)
            )
        if (
            self.automation.max_correction_per_cycle_mu
            and self.automation.max_correction_per_cycle_mu < device.increment_mu
        ):
            errors.append(
                "automation.max_correction_per_cycle_units is smaller than the pump's dose increment (%s U)"
                % format_units(device.increment_mu, 3)
            )
        return errors


def load_profile(path, schema_path=None):
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except ValueError as exc:
            raise ProfileValidationError(["profile file is not valid JSON: %s" % exc])
    return TherapyProfile(data, schema_path)
