"""Mock hardware: a continuous glucose monitor (CGM) and an insulin delivery motor.

These stand in for real device drivers. Anything a real device would report
(sensor range, trend-arrow meanings, dose increment, hardware limits) is
*fetched* from these mocks by the controller rather than hard-coded in it.

The CGM runs a very simple patient model (floating point is fine here: this is
the simulated body, not the safety logic) so the controller's decisions have a
visible effect on glucose. Both mocks have fault-injection hooks for testing.
"""

import math

from .models import DeliveryResult, Dose, GlucoseReading

# Trend arrows as reported by this (mock) CGM model:
#   name -> (representative rate in mg/dL/min as an exact decimal string,
#            lowest rate that still shows this arrow).
# The controller only receives the representative rates, via get_trend_arrows().
TREND_ARROWS = (
    ("DOUBLE_UP", "3", 2.5),
    ("SINGLE_UP", "2", 1.5),
    ("FORTY_FIVE_UP", "1", 0.5),
    ("FLAT", "0", -0.5),
    ("FORTY_FIVE_DOWN", "-1", -1.5),
    ("SINGLE_DOWN", "-2", -2.5),
    ("DOUBLE_DOWN", "-3", None),
)
SENSOR_MIN_MG_DL = 40
SENSOR_MAX_MG_DL = 400
READING_INTERVAL_S = 300


class Xorshift32:
    """Tiny deterministic PRNG (same sequence on CPython and MicroPython)."""

    def __init__(self, seed):
        self._state = (seed or 1) & 0xFFFFFFFF or 1

    def next_u32(self):
        x = self._state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self._state = x
        return x

    def uniform(self, low, high):
        return low + (high - low) * (self.next_u32() / 4294967296.0)


def _patient_iob(t_min, duration, peak, delay):
    """Fraction of a dose still active in the *simulated body* (exponential model, floats)."""
    t = t_min - delay
    if t <= 0:
        return 1.0
    if t >= duration:
        return 0.0
    tau = peak * (1 - peak / duration) / (1 - 2 * peak / duration)
    a = 2 * tau / duration
    s = 1 / (1 - a + (1 + a) * math.exp(-duration / tau))
    return 1 - s * (1 - a) * (
        (t * t / (tau * duration * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
    )


def arrow_for_rate(rate):
    for name, _rate, lowest in TREND_ARROWS:
        if lowest is None or rate >= lowest:
            return name
    return TREND_ARROWS[-1][0]


class MockCGM:
    """Mock CGM exposing current glucose, history, trend direction, sensor range and arrow meanings."""

    def __init__(
        self,
        clock,
        starting_glucose=120.0,
        insulin_sensitivity=50.0,
        carb_ratio=10.0,
        basal_need_mu_per_hour=0,
        insulin_duration_min=360,
        insulin_peak_min=75,
        insulin_delay_min=10,
        noise_mg_dl=2.0,
        seed=1,
        backfill_minutes=30,
    ):
        self._clock = clock
        self._true_glucose = float(starting_glucose)
        self._isf = float(insulin_sensitivity)
        self._carb_ratio = float(carb_ratio)
        self._insulin = (insulin_duration_min, insulin_peak_min, insulin_delay_min)
        if callable(basal_need_mu_per_hour):
            self._basal_need = basal_need_mu_per_hour
        else:
            self._basal_need = lambda _t, v=basal_need_mu_per_hour: v
        self._noise = noise_mg_dl
        self._rng = Xorshift32(seed)
        self._doses = []
        self._meals = []  # [remaining mg/dL, mg/dL per min]
        self._history = []
        self.dropout = False  # when True no new readings are produced (data goes stale)
        self.raise_errors = False
        now = clock.now()
        for k in range(backfill_minutes // 5, 0, -1):
            self._record_reading(now - k * READING_INTERVAL_S)
        self._record_reading(now)

    # ---- Mock device API used by the pump -----------------------------------------

    def get_current_glucose(self):
        self._maybe_raise()
        return self._history[-1] if self._history else None

    def get_glucose_history(self, minutes=180):
        self._maybe_raise()
        cutoff = self._clock.now() - minutes * 60
        return [r for r in self._history if r.timestamp >= cutoff]

    def get_glucose_trend(self):
        self._maybe_raise()
        return self._history[-1].trend if self._history else None

    def get_trend_arrows(self):
        """Meaning of each trend arrow: representative rate of change in mg/dL per minute."""
        self._maybe_raise()
        return {name: rate for name, rate, _lowest in TREND_ARROWS}

    def get_sensor_info(self):
        self._maybe_raise()
        return {"min_mg_dl": SENSOR_MIN_MG_DL, "max_mg_dl": SENSOR_MAX_MG_DL}

    # ---- Simulation hooks ----------------------------------------------------------

    @property
    def true_glucose(self):
        return self._true_glucose

    def prime_basal(self):
        """Pretend basal insulin was already running so the patient starts at steady state."""
        now = self._clock.now()
        duration, _peak, delay = self._insulin
        for k in range(1, (duration + delay) // 5 + 1):
            t = now - k * 300
            self._doses.append(Dose(t, self._basal_need(t) * 5 / 60.0))

    def notify_insulin(self, mu):
        if mu > 0:
            self._doses.append(Dose(self._clock.now(), mu))

    def add_meal(self, carbs_g, absorption_min=180.0):
        rise = carbs_g * self._isf / self._carb_ratio
        self._meals.append([rise, rise / absorption_min])

    def inject_reading(self, mg_dl, trend=None, timestamp=None):
        self._history.append(
            GlucoseReading(
                self._clock.now() if timestamp is None else timestamp, mg_dl, trend
            )
        )

    def step(self, minutes):
        """Call after the clock has advanced `minutes`; updates the patient and records a reading."""
        now = self._clock.now()
        self._true_glucose += (
            self._basal_need(now) / 1000.0 * self._isf / 60.0 * minutes
        )  # endogenous glucose output
        self._true_glucose += self._meal_effect(minutes)
        self._true_glucose -= self._insulin_effect(now - minutes * 60, now)
        self._true_glucose = max(20.0, min(600.0, self._true_glucose))
        if not self.dropout:
            self._record_reading(now)

    def _maybe_raise(self):
        if self.raise_errors:
            raise OSError("CGM transmitter not responding")

    def _meal_effect(self, minutes):
        total, active = 0.0, []
        for remaining, rate in self._meals:
            applied = min(remaining, rate * minutes)
            total += applied
            if remaining - applied > 0:
                active.append([remaining - applied, rate])
        self._meals = active
        return total

    def _insulin_effect(self, start, end):
        duration, peak, delay = self._insulin
        drop = 0.0
        kept = []
        for dose in self._doses:
            before = _patient_iob(
                (start - dose.timestamp) / 60.0, duration, peak, delay
            )
            after = _patient_iob((end - dose.timestamp) / 60.0, duration, peak, delay)
            drop += dose.mu / 1000.0 * self._isf * (before - after)
            if after > 0:
                kept.append(dose)
        self._doses = kept
        return drop

    def _record_reading(self, timestamp):
        sensed = self._true_glucose + self._rng.uniform(-self._noise, self._noise)
        # Real sensors report LO/HI outside their range; this mock reports one step beyond it.
        value = min(max(int(round(sensed)), SENSOR_MIN_MG_DL - 1), SENSOR_MAX_MG_DL + 1)
        rate = 0.0
        if self._history:
            prev = self._history[-1]
            elapsed = (timestamp - prev.timestamp) / 60.0
            if elapsed > 0:
                rate = (value - prev.mg_dl) / elapsed
        self._history.append(GlucoseReading(timestamp, value, arrow_for_rate(rate)))
        if len(self._history) > 100:
            self._history.pop(0)


class MockInsulinDelivery:
    """Mock pump motor with a reservoir and fault-injection hooks. All amounts in integer milliunits."""

    def __init__(
        self,
        clock,
        reservoir_mu=200000,
        cgm=None,
        increment_mu=50,
        max_single_delivery_mu=25000,
        max_hourly_mu=30000,
        max_daily_mu=150000,
        reservoir_capacity_mu=300000,
    ):
        self._clock = clock
        self.reservoir_mu = reservoir_mu
        self.info = {
            "increment_mu": increment_mu,
            "max_single_delivery_mu": max_single_delivery_mu,
            "max_hourly_mu": max_hourly_mu,
            "max_daily_mu": max_daily_mu,
            "reservoir_capacity_mu": reservoir_capacity_mu,
        }
        self.delivery_log = []
        self._cgm = cgm
        self.occluded = False  # every delivery fails until cleared
        self.fail_next = False  # next delivery fails
        self.raise_next = False  # next call raises (driver crash)
        self.over_deliver_mu = None  # next delivery delivers this much extra
        self.partial_deliver_mu = None  # next delivery delivers only this much
        self.invalid_next = False  # next call returns garbage

    def get_pump_info(self):
        return dict(self.info)

    def get_reservoir_mu(self):
        return self.reservoir_mu

    def give_insulin(self, mu):
        if self.raise_next:
            self.raise_next = False
            raise OSError("motor controller timeout")
        if self.invalid_next:
            self.invalid_next = False
            return {"ok": True}
        if type(mu) is not int or mu < 0:
            return DeliveryResult(False, 0, "Invalid dose rejected")
        if mu == 0:
            return DeliveryResult(True, 0, "Nothing to deliver")
        if mu % self.info["increment_mu"]:
            return DeliveryResult(
                False, 0, "Dose is not a multiple of the motor increment"
            )
        if self.occluded:
            return DeliveryResult(False, 0, "Occlusion detected")
        if self.fail_next:
            self.fail_next = False
            return DeliveryResult(False, 0, "Motor error")
        if mu > self.reservoir_mu:
            return DeliveryResult(False, 0, "Insufficient insulin in reservoir")

        actual = mu
        if self.over_deliver_mu is not None:
            actual, self.over_deliver_mu = mu + self.over_deliver_mu, None
        elif self.partial_deliver_mu is not None:
            actual, self.partial_deliver_mu = min(mu, self.partial_deliver_mu), None
        actual = min(actual, self.reservoir_mu)
        self.reservoir_mu -= actual
        self.delivery_log.append(Dose(self._clock.now(), actual))
        if self._cgm is not None:
            self._cgm.notify_insulin(actual)
        return DeliveryResult(True, actual, "Delivered %d mU" % actual)
