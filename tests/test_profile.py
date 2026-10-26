import json
import os
import tempfile
import unittest

from insulin_pump.exact import Q
from insulin_pump.profile import (
    ProfileValidationError,
    TherapyProfile,
    get_validator,
    load_profile,
)
from insulin_pump.schedule import Schedule, ScheduleError, Segment, parse_time_of_day
from insulin_pump.schema import SchemaError, Validator

from .helpers import base_profile_data, make_profile, seg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "profiles", "example_profile.json")
SCHEMA = os.path.join(ROOT, "insulin_pump", "schemas", "profile.schema.json")


def segment(start, end, value):
    return Segment(parse_time_of_day(start), parse_time_of_day(end, is_end=True), value)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ScheduleTests(unittest.TestCase):
    def test_default_with_partial_day_segments(self):
        s = Schedule("cr", (segment("06:00", "11:00", 9),), default=12)
        self.assertEqual(s.value_at_minute(5 * 60 + 59), 12)
        self.assertEqual(s.value_at_minute(6 * 60), 9)
        self.assertEqual(s.value_at_minute(10 * 60 + 59), 9)
        self.assertEqual(s.value_at_minute(11 * 60), 12)
        self.assertEqual(
            s.value_at(86400 + 7 * 3600), 9
        )  # timestamps on later days use local time of day

    def test_segment_wrapping_midnight(self):
        s = Schedule("t", (segment("22:00", "06:00", 120),), default=110)
        for minute, expected in (
            (23 * 60, 120),
            (0, 120),
            (5 * 60 + 59, 120),
            (6 * 60, 110),
            (21 * 60 + 59, 110),
        ):
            self.assertEqual(s.value_at_minute(minute), expected)

    def test_overlapping_segments_rejected(self):
        with self.assertRaisesRegex(ScheduleError, "overlap"):
            Schedule(
                "b",
                (segment("06:00", "12:00", 1), segment("11:00", "14:00", 2)),
                default=1,
            )
        with self.assertRaisesRegex(ScheduleError, "overlap"):
            Schedule(
                "b",
                (segment("22:00", "06:00", 1), segment("05:00", "07:00", 2)),
                default=1,
            )

    def test_adjacent_segments_allowed(self):
        s = Schedule(
            "b", (segment("06:00", "12:00", 1), segment("12:00", "14:00", 2)), default=3
        )
        self.assertEqual(s.value_at_minute(12 * 60), 2)

    def test_no_default_requires_full_coverage(self):
        with self.assertRaisesRegex(ScheduleError, "12:00-18:00"):
            Schedule("b", (segment("00:00", "12:00", 1), segment("18:00", "24:00", 2)))
        full = Schedule(
            "b", (segment("06:00", "18:00", 1), segment("18:00", "06:00", 2))
        )
        self.assertEqual(full.value_at_minute(3 * 60), 2)

    def test_bad_times_and_zero_length(self):
        for bad in ("25:00", "7:00", "12:60", "noon", 7, None):
            with self.assertRaises(ScheduleError, msg=repr(bad)):
                parse_time_of_day(bad)
        with self.assertRaisesRegex(ScheduleError, "zero length"):
            segment("06:00", "06:00", 1)
        with self.assertRaises(ScheduleError):
            Schedule("b")


class ProfileTests(unittest.TestCase):
    def test_example_profile_loads_with_exact_values(self):
        p = load_profile(EXAMPLE)
        self.assertEqual(p.at_minute(7 * 60).carb_ratio, 9)
        self.assertEqual(p.at_minute(13 * 60).carb_ratio, 12)
        self.assertEqual(p.at_minute(23 * 60).low_threshold, 80)
        self.assertEqual(
            p.at_minute(4 * 60).basal_rate, 1000
        )  # U/hr stored as exact mU/hr
        self.assertEqual(p.automation.correction_fraction, Q(3, 10))
        self.assertEqual(p.limits.max_bolus_mu, 10000)
        self.assertEqual(p.insulin.curve, "envelope")

    def test_schema_defaults_fill_omitted_settings(self):
        p = make_profile()
        schema = read_json(SCHEMA)
        default_bolus = schema["properties"]["limits"]["properties"]["max_bolus_units"][
            "default"
        ]
        self.assertEqual(p.limits.max_bolus_mu, default_bolus * 1000)
        self.assertEqual(p.cgm.fallback_basal_minutes, 0)  # default action: no insulin
        self.assertEqual(p.insulin.duration_minutes, 360)

    def test_decimal_values_are_exact(self):
        p = make_profile(basal_rate={"default": 0.025}, carb_ratio={"default": 7.5})
        self.assertEqual(p.basal_rate.default, 25)
        self.assertEqual(p.carb_ratio.default, Q(15, 2))
        with self.assertRaisesRegex(ProfileValidationError, "decimal place"):
            make_profile(basal_rate={"default": 0.0255})

    def test_values_outside_hard_limits_rejected(self):
        with self.assertRaisesRegex(
            ProfileValidationError, "carb_ratio.default must be at least 2"
        ):
            make_profile(carb_ratio={"default": 0.5})
        with self.assertRaisesRegex(ProfileValidationError, "correction_factor"):
            make_profile(
                correction_factor={
                    "default": 50,
                    "segments": [seg("22:00", "06:00", 1000)],
                }
            )
        with self.assertRaisesRegex(
            ProfileValidationError, "duration_minutes must be at least 300"
        ):
            make_profile(insulin={"duration_minutes": 180})

    def test_wrong_types_rejected(self):
        for bad in ("12", True, None, [12]):
            with self.assertRaises(ProfileValidationError, msg=repr(bad)):
                make_profile(carb_ratio={"default": bad})
        with self.assertRaisesRegex(ProfileValidationError, "integer"):
            make_profile(target={"default": 110.5})

    def test_cross_field_checked_at_every_time_of_day(self):
        # Only wrong overnight: night low threshold (115) is above the all-day target (110).
        with self.assertRaises(ProfileValidationError) as ctx:
            make_profile(
                low_threshold={"default": 70, "segments": [seg("22:00", "06:00", 115)]}
            )
        self.assertTrue(
            any("At 22:00" in e and "low threshold" in e for e in ctx.exception.errors)
        )

    def test_other_cross_field_rules(self):
        with self.assertRaisesRegex(ProfileValidationError, "correct-above"):
            make_profile(
                target={"default": 110, "segments": [seg("12:00", "13:00", 160)]}
            )
        with self.assertRaisesRegex(ProfileValidationError, "urgent low"):
            make_profile(low_threshold={"default": 65}, alarms={"urgent_low": 66})
        with self.assertRaisesRegex(ProfileValidationError, "max basal"):
            make_profile(
                basal_rate={"default": 1, "segments": [seg("04:00", "07:00", 3.5)]}
            )
        with self.assertRaisesRegex(ProfileValidationError, "max_iob"):
            make_profile(limits={"max_bolus_units": 10, "max_iob_units": 5})
        with self.assertRaisesRegex(ProfileValidationError, "max_daily"):
            make_profile(limits={"max_daily_units": 20})
        with self.assertRaisesRegex(ProfileValidationError, "max_correction_per_cycle"):
            make_profile(
                limits={"max_bolus_units": 0.3},
                automation={"max_correction_per_cycle_units": 0.5},
            )
        with self.assertRaisesRegex(ProfileValidationError, "rate_window"):
            make_profile(cgm={"rate_window_minutes": 30, "history_window_minutes": 20})

    def test_all_errors_reported_together(self):
        with self.assertRaises(ProfileValidationError) as ctx:
            make_profile(
                carb_ratio={"default": 0},
                insulin={"duration_minutes": 20},
                limits={"max_bolus_units": -1},
            )
        self.assertGreaterEqual(len(ctx.exception.errors), 3)

    def test_unknown_and_missing_keys_rejected(self):
        data = base_profile_data()
        data["carb_raito"] = data.pop("carb_ratio")
        with self.assertRaises(ProfileValidationError) as ctx:
            TherapyProfile(data)
        message = str(ctx.exception)
        self.assertIn("carb_raito", message)
        self.assertIn("carb_ratio", message)
        with self.assertRaisesRegex(ProfileValidationError, "max_bolus"):
            make_profile(limits={"max_bolus": 5})
        with self.assertRaisesRegex(ProfileValidationError, "valu"):
            make_profile(
                carb_ratio={
                    "default": 10,
                    "segments": [{"start": "06:00", "end": "11:00", "valu": 9}],
                }
            )

    def _load_text(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return load_profile(path)

    def test_json_nan_infinity_and_garbage_rejected(self):
        text = read_text(EXAMPLE)
        for replacement in (
            '"default": NaN',
            '"default": Infinity',
            '"default": 1e400',
        ):
            with self.assertRaises(ProfileValidationError, msg=replacement):
                self._load_text(text.replace('"default": 12', replacement))
        with self.assertRaises(ProfileValidationError):
            self._load_text("{not json")

    def test_input_dict_not_modified(self):
        data = base_profile_data()
        TherapyProfile(data)
        self.assertNotIn("limits", data)


class SchemaTests(unittest.TestCase):
    def test_unsupported_keyword_refused(self):
        with self.assertRaisesRegex(SchemaError, "oneOf"):
            Validator({"type": "object", "properties": {"a": {"oneOf": []}}})

    def test_conflicting_ref_siblings_refused(self):
        schema = {
            "$defs": {"n": {"type": "number", "minimum": 1}},
            "properties": {"a": {"$ref": "#/$defs/n", "minimum": 2}},
        }
        with self.assertRaisesRegex(SchemaError, "minimum"):
            Validator(schema)

    def test_ref_siblings_both_apply(self):
        v = Validator(
            {
                "$defs": {"n": {"type": "number"}},
                "properties": {"a": {"$ref": "#/$defs/n", "maximum": 5}},
            }
        )
        self.assertEqual(v.validate({"a": 4}), [])
        self.assertTrue(v.validate({"a": 6}))
        self.assertTrue(v.validate({"a": "x"}))

    def test_bundled_schema_loads(self):
        self.assertIsInstance(get_validator(), Validator)


try:
    import jsonschema
except ImportError:  # optional dev dependency (requirements.txt)
    jsonschema = None


@unittest.skipIf(jsonschema is None, "jsonschema not installed")
class StandardJsonSchemaTests(unittest.TestCase):
    """The schema is standard JSON Schema 2020-12: editors/tools agree with the firmware's validator."""

    def setUp(self):
        self.schema = read_json(SCHEMA)
        jsonschema.Draft202012Validator.check_schema(self.schema)
        self.reference = jsonschema.Draft202012Validator(self.schema)
        self.ours = get_validator()

    def test_example_profile_valid_for_both(self):
        data = read_json(EXAMPLE)
        self.assertEqual(list(self.reference.iter_errors(data)), [])
        self.assertEqual(self.ours.validate(data), [])

    def test_invalid_profiles_rejected_by_both(self):
        cases = [
            {"carb_ratio": {"default": 1}},
            {"target": {"default": "110"}},
            {"limits": {"max_bolus_units": 30}},
            {"limits": {"surprise": 1}},
            {"insulin": {"curve": "cubic"}},
            {"basal_rate": {"default": 1, "segments": [seg("6:00", "11:00", 1)]}},
            {
                "basal_rate": {
                    "default": 1,
                    "segments": [{"start": "06:00", "end": "11:00"}],
                }
            },
            {"cgm": {"fallback_basal_minutes": -5}},
        ]
        for override in cases:
            data = base_profile_data()
            data.update(override)
            self.assertTrue(list(self.reference.iter_errors(data)), override)
            self.assertTrue(self.ours.validate(data), override)


if __name__ == "__main__":
    unittest.main()
