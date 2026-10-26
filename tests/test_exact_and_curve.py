import math
import unittest

from insulin_pump.exact import (
    DecimalError,
    Q,
    clamp,
    floor_to_increment,
    format_decimal,
    format_units,
    parse_decimal,
    to_scaled_int,
)
from insulin_pump.insulin_curve import PPB, CurveError, InsulinCurve


class RationalTests(unittest.TestCase):
    def test_exact_where_floats_are_not(self):
        self.assertNotEqual(0.1 + 0.2, 0.3)
        self.assertEqual(
            parse_decimal("0.1") + parse_decimal("0.2"), parse_decimal("0.3")
        )
        self.assertEqual(Q(1, 3) * 3, 1)

    def test_normalisation_and_comparison(self):
        self.assertEqual((Q(2, -4).n, Q(2, -4).d), (-1, 2))
        self.assertTrue(Q(1, 3) < Q(1, 2) <= Q(2, 4) < 1)
        self.assertTrue(Q(3, 2) > 1 and Q(3, 2) >= Q(6, 4))
        self.assertEqual(hash(Q(1, 2)), hash(Q(2, 4)))
        self.assertEqual(clamp(Q(5, 4), 0, 1), 1)

    def test_floor_and_ceil(self):
        self.assertEqual((Q(7, 2).floor(), Q(7, 2).ceil()), (3, 4))
        self.assertEqual((Q(-7, 2).floor(), Q(-7, 2).ceil()), (-4, -3))

    def test_rejects_floats_and_bools(self):
        for bad in (0.5, True, "1"):
            with self.assertRaises(TypeError):
                Q(1) + bad

    def test_parse_decimal(self):
        self.assertEqual(parse_decimal("0.85"), Q(17, 20))
        self.assertEqual(parse_decimal(0.85), Q(17, 20))
        self.assertEqual(parse_decimal(12), Q(12))
        self.assertEqual(parse_decimal("1e-3"), Q(1, 1000))
        self.assertEqual(parse_decimal("-2.50"), Q(-5, 2))
        self.assertEqual(parse_decimal(1.5e1, 0), Q(15))
        for bad in ("abc", "", "1.2.3", float("nan"), float("inf"), None, True):
            with self.assertRaises(DecimalError, msg=repr(bad)):
                parse_decimal(bad)
        with self.assertRaisesRegex(DecimalError, "decimal place"):
            parse_decimal(0.0255, 3)

    def test_scaled_int(self):
        self.assertEqual(to_scaled_int(0.025, 1000, 3), 25)
        self.assertEqual(to_scaled_int("12.5", 1000, 3), 12500)
        with self.assertRaises(DecimalError):
            to_scaled_int(0.0001, 1000, 3)

    def test_floor_to_increment_never_rounds_up(self):
        self.assertEqual(floor_to_increment(Q(1049), 50), 1000)
        self.assertEqual(floor_to_increment(1050, 50), 1050)
        self.assertEqual(floor_to_increment(Q(4999, 100), 50), 0)
        self.assertEqual(floor_to_increment(-100, 50), 0)

    def test_formatting(self):
        self.assertEqual(format_units(1250), "1.25")
        self.assertEqual(format_units(5), "0.01")
        self.assertEqual(format_units(-500), "-0.50")
        self.assertEqual(format_decimal(Q(17, 20)), "0.85")
        self.assertEqual(format_decimal(Q(1, 3), 3), "0.333")


def reference_iob(t, td, tp, delay):
    """Floating-point reference: oref0 iobCalcExponential / LoopKit ExponentialInsulinModel."""
    t = t - delay
    if t <= 0:
        return 1.0
    if t >= td:
        return 0.0
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * math.exp(-td / tau))
    return 1 - s * (1 - a) * (
        (t * t / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
    )


class InsulinCurveTests(unittest.TestCase):
    CASES = ((360, 75, 10), (300, 55, 0), (480, 120, 30), (300, 35, 0))

    def test_exponential_matches_reference_and_never_undercounts(self):
        for td, tp, delay in self.CASES:
            table = InsulinCurve("exponential", td, tp, delay).table()
            for minute, ppb in enumerate(table):
                diff = ppb / PPB - reference_iob(minute, td, tp, delay)
                self.assertGreaterEqual(diff, -1e-12, (td, tp, delay, minute))
                self.assertLess(diff, 3e-9, (td, tp, delay, minute))

    def test_linear_is_exact(self):
        curve = InsulinCurve("linear", 300, 75, 0)
        self.assertEqual(curve.remaining_ppb(150 * 60), PPB // 2)
        self.assertEqual(curve.iob_mu(1000, 150 * 60), 500)

    def test_envelope_is_never_below_either_model(self):
        for td, tp, delay in self.CASES:
            env = InsulinCurve("envelope", td, tp, delay).table()
            lin = InsulinCurve("linear", td, tp, delay).table()
            exp = InsulinCurve("exponential", td, tp, delay).table()
            self.assertTrue(all(e >= max(l, x) for e, l, x in zip(env, lin, exp)))
        # Loop's adult preset: early on the exponential curve is higher; later the linear one is.
        lin = InsulinCurve("linear", 360, 75, 10).table()
        exp = InsulinCurve("exponential", 360, 75, 10).table()
        self.assertGreater(exp[30], lin[30])
        self.assertGreater(lin[185], exp[185])

    def test_shape(self):
        for model in InsulinCurve.MODELS:
            table = InsulinCurve(model, 360, 75, 10).table()
            self.assertEqual((table[0], table[-1]), (PPB, 0))
            self.assertTrue(all(b <= a for a, b in zip(table, table[1:])))

    def test_lookup_rounds_toward_more_iob(self):
        curve = InsulinCurve("envelope", 360, 75, 10)
        self.assertEqual(curve.iob_mu(1000, -60), 1000)  # future-dated: counts in full
        self.assertEqual(curve.iob_mu(1000, 0), 1000)
        self.assertEqual(
            curve.iob_mu(1000, 59 * 60 + 59), curve.iob_mu(1000, 59 * 60)
        )  # whole minutes, rounded down
        self.assertEqual(
            curve.iob_mu(1, 359 * 60), 1
        )  # any remaining fraction rounds up
        self.assertEqual(curve.iob_mu(1000, 370 * 60), 0)

    def test_invalid_parameters(self):
        with self.assertRaises(CurveError):
            InsulinCurve("exponential", 120, 60, 0)  # peak must be < duration / 2
        with self.assertRaises(CurveError):
            InsulinCurve("cubic", 360, 75, 0)
        with self.assertRaises(CurveError):
            InsulinCurve("linear", 360.0, 75, 0)


if __name__ == "__main__":
    unittest.main()
