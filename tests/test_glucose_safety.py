import unittest

from insulin_pump.exact import Q
from insulin_pump.glucose import assess_glucose
from insulin_pump.mocks import TREND_ARROWS
from insulin_pump.models import (
    DeviceInfoError,
    Dose,
    DoseKind,
    GlucoseReading,
    PumpInfo,
    SensorInfo,
    SensorStatus,
)
from insulin_pump.safety import DeliveryLedger, SafetyMonitor

from .helpers import make_profile

NOW = 12 * 3600
MIN = 60
ARROWS = {name: rate for name, rate, _ in TREND_ARROWS}
MAX_RATE = Q(5)
SENSOR = SensorInfo({"min_mg_dl": 40, "max_mg_dl": 400}, ARROWS, MAX_RATE)
PUMP = PumpInfo(
    {
        "increment_mu": 50,
        "max_single_delivery_mu": 25000,
        "max_hourly_mu": 30000,
        "max_daily_mu": 150000,
        "reservoir_capacity_mu": 300000,
    }
)


def readings(values, spacing=5, trend="FLAT"):
    n = len(values)
    return [
        GlucoseReading(NOW - spacing * MIN * (n - 1 - i), v, trend)
        for i, v in enumerate(values)
    ]


class GlucoseAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.cgm = make_profile().cgm

    def assess(self, values, trend="FLAT", now=NOW, cgm=None, sensor=SENSOR):
        h = readings(values, trend=trend)
        return assess_glucose(
            h[-1] if h else None, h, trend, now, cgm or self.cgm, sensor
        )

    def test_exact_rate_and_prediction(self):
        a = self.assess([100, 110, 120, 130], "SINGLE_UP")
        self.assertTrue(a.reliable)
        self.assertEqual(a.slope, Q(2))
        self.assertEqual(a.rate, Q(2))
        self.assertEqual(a.predicted, 190)

    def test_prediction_rounds_down(self):
        a = self.assess(
            [100, 101, 101], "FLAT"
        )  # slope 0.1 mg/dL/min, arrow 0 -> lower = 0
        self.assertEqual(a.rate, 0)
        a = self.assess(
            [100, 101, 101],
            "FLAT",
            cgm=make_profile(cgm={"rate_combination": "average"}).cgm,
        )
        self.assertEqual(a.rate, Q(1, 20))
        self.assertEqual(
            a.predicted, 102
        )  # 101 + 0.05 * 30 = 102.5 -> 102 (rounded down)

    def test_lower_rate_wins_by_default(self):
        a = self.assess([110, 110, 110], "SINGLE_DOWN")
        self.assertEqual((a.slope, a.arrow_rate, a.rate), (0, -2, -2))
        self.assertEqual(a.predicted, 50)

    def test_trend_arrow_meaning_comes_from_cgm(self):
        custom = SensorInfo(
            {"min_mg_dl": 40, "max_mg_dl": 400}, {"FLAT": "0", "DOWN": "-1.5"}, MAX_RATE
        )
        a = self.assess([120], "DOWN", sensor=custom)
        self.assertEqual(a.rate, Q(-3, 2))
        self.assertEqual(a.predicted, 75)
        unknown = self.assess([120], "SIDEWAYS", sensor=custom)
        self.assertEqual(unknown.status, SensorStatus.NO_TREND)
        self.assertFalse(unknown.reliable)

    def test_no_data_and_invalid_values(self):
        self.assertEqual(self.assess([]).status, SensorStatus.NO_DATA)
        for bad in (
            GlucoseReading(NOW, 120.0),
            GlucoseReading(NOW, True),
            GlucoseReading(float(NOW), 120),
            "120",
        ):
            self.assertEqual(
                assess_glucose(bad, [], "FLAT", NOW, self.cgm, SENSOR).status,
                SensorStatus.NO_DATA,
            )

    def test_missing_sensor_info_is_unreliable(self):
        self.assertEqual(
            self.assess([120, 120, 120], sensor=None).status, SensorStatus.NO_DATA
        )

    def test_stale_and_future(self):
        self.assertEqual(
            self.assess([120], now=NOW + 20 * MIN).status, SensorStatus.STALE
        )
        self.assertEqual(
            self.assess([120], now=NOW - 5 * MIN).status, SensorStatus.FUTURE_TIMESTAMP
        )

    def test_stale_low_reading_still_counts_as_low(self):
        a = self.assess([60], now=NOW + 30 * MIN)
        self.assertEqual(a.status, SensorStatus.STALE)
        self.assertTrue(a.is_low(70))

    def test_sensor_range_from_device(self):
        lo = self.assess([39])
        self.assertEqual(lo.status, SensorStatus.SENSOR_LOW)
        self.assertTrue(lo.indicates_low(70))
        self.assertEqual(self.assess([401]).status, SensorStatus.SENSOR_HIGH)

    def test_implausible_jump(self):
        a = self.assess([120, 120, 200])
        self.assertEqual(a.status, SensorStatus.IMPLAUSIBLE)
        self.assertTrue(
            self.assess([120, 120, 60]).indicates_low(70)
        )  # a sudden low is still treated as low

    def test_predicted_low(self):
        a = self.assess([120, 110, 100, 90], "SINGLE_DOWN")
        self.assertFalse(a.is_low(70))
        self.assertTrue(a.indicates_low(70))


class DeviceInfoTests(unittest.TestCase):
    def test_invalid_pump_info(self):
        for bad in ({}, {"increment_mu": 0}, {"increment_mu": 50.0}):
            with self.assertRaises(DeviceInfoError):
                PumpInfo(bad)
        with self.assertRaisesRegex(DeviceInfoError, "multiple"):
            PumpInfo(
                {
                    "increment_mu": 30,
                    "max_single_delivery_mu": 25000,
                    "max_hourly_mu": 30000,
                    "max_daily_mu": 150000,
                    "reservoir_capacity_mu": 300000,
                }
            )

    def test_invalid_sensor_info(self):
        with self.assertRaises(DeviceInfoError):
            SensorInfo({"min_mg_dl": 40, "max_mg_dl": 400}, {}, MAX_RATE)
        with self.assertRaises(DeviceInfoError):
            SensorInfo({"min_mg_dl": 40, "max_mg_dl": 400}, {"FLAT": "zero"}, MAX_RATE)
        with self.assertRaises(DeviceInfoError):
            SensorInfo({"min_mg_dl": 400, "max_mg_dl": 40}, ARROWS, MAX_RATE)
        with self.assertRaisesRegex(DeviceInfoError, "implausible"):
            SensorInfo({"min_mg_dl": 40, "max_mg_dl": 400}, {"ROCKET": "8"}, MAX_RATE)


class SafetyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.ledger = DeliveryLedger()
        self.monitor = SafetyMonitor(self.ledger, PUMP)
        self.profile = make_profile(
            limits={
                "max_bolus_units": 5,
                "max_basal_rate_units_per_hour": 2.4,
                "max_iob_units": 6,
                "max_hourly_units": 8,
                "max_daily_units": 40,
            }
        )

    def authorize(self, mu, kind=DoseKind.BOLUS):
        return self.monitor.authorize(mu, kind, self.profile, NOW, 300)

    def test_invalid_requests_get_nothing(self):
        for bad in (float("nan"), 1.5, -1, True, "2", None):
            self.assertEqual(self.authorize(bad).approved_mu, 0, repr(bad))
        self.assertEqual(
            self.monitor.authorize(1000, "mystery", self.profile, NOW, 300).approved_mu,
            0,
        )
        self.assertEqual(
            self.monitor.authorize(1000, DoseKind.BOLUS, None, NOW, 300).approved_mu, 0
        )

    def test_rounds_down_to_pump_increment(self):
        self.assertEqual(self.authorize(1049).approved_mu, 1000)
        self.assertEqual(self.authorize(Q(10999, 10)).approved_mu, 1050)

    def test_max_bolus(self):
        v = self.authorize(7000)
        self.assertEqual(v.approved_mu, 5000)
        self.assertTrue(v.limited)
        self.assertFalse(self.authorize(4950).limited)

    def test_basal_cap_per_cycle(self):
        self.assertEqual(self.authorize(1000, DoseKind.BASAL).approved_mu, 200)

    def test_iob_cap_ignores_basal(self):
        self.ledger.record(Dose(NOW - 10 * MIN, 4000, DoseKind.BASAL))
        self.ledger.record(Dose(NOW, 4000, DoseKind.BOLUS))
        self.assertEqual(self.authorize(4000).approved_mu, 0)  # hourly: 8 - 8 used
        self.ledger = DeliveryLedger()
        self.monitor = SafetyMonitor(self.ledger, PUMP)
        self.ledger.record(Dose(NOW - 3 * 3600, 5000, DoseKind.BOLUS))
        iob = self.ledger.insulin_on_board(NOW, self.profile.curve)
        self.assertEqual(self.authorize(5000).approved_mu, (6000 - iob) // 50 * 50)

    def test_daily_cap(self):
        for h in range(2, 20, 2):
            self.ledger.record(Dose(NOW - h * 3600, 4000, DoseKind.BASAL))
        self.assertEqual(self.authorize(5000).approved_mu, 4000)  # 40 - 36

    def test_pump_hardware_limit_applies(self):
        small = PumpInfo(
            {
                "increment_mu": 50,
                "max_single_delivery_mu": 2000,
                "max_hourly_mu": 30000,
                "max_daily_mu": 150000,
                "reservoir_capacity_mu": 300000,
            }
        )
        monitor = SafetyMonitor(self.ledger, small)
        self.assertEqual(
            monitor.authorize(5000, DoseKind.BOLUS, self.profile, NOW, 300).approved_mu,
            2000,
        )

    def test_future_dated_doses_count_fully(self):
        self.ledger.record(Dose(NOW + 30 * MIN, 3000, DoseKind.BOLUS))
        self.assertEqual(self.ledger.insulin_on_board(NOW, self.profile.curve), 3000)
        self.assertEqual(self.ledger.total_since(NOW, 3600), 3000)

    def test_ledger_prunes_old_doses(self):
        self.ledger.record(Dose(NOW - 30 * 3600, 1000, DoseKind.BASAL))
        self.ledger.record(Dose(NOW, 1000, DoseKind.BASAL))
        self.ledger.prune(NOW, 25 * 3600)
        self.assertEqual(len(self.ledger.doses), 1)
        self.assertEqual(
            self.ledger.totals_mu[DoseKind.BASAL], 2000
        )  # lifetime totals are kept


if __name__ == "__main__":
    unittest.main()
