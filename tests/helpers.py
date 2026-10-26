from insulin_pump.clock import SimulatedClock
from insulin_pump.controller import InsulinPumpController
from insulin_pump.mocks import TREND_ARROWS, MockInsulinDelivery
from insulin_pump.models import GlucoseReading
from insulin_pump.profile import TherapyProfile

START = 12 * 3600  # day 0, 12:00 local time
MIN = 60


def base_profile_data():
    return {
        "name": "Test",
        "basal_rate": {"default": 1.2},
        "carb_ratio": {"default": 10},
        "correction_factor": {"default": 50},
        "target": {"default": 110},
        "correct_above": {"default": 150},
        "low_threshold": {"default": 70},
    }


def make_profile(**overrides):
    """Build a validated profile from the base data. Schedules are replaced; settings
    sections (dicts without 'default'/'segments') are merged."""
    data = base_profile_data()
    for key, value in overrides.items():
        if (
            isinstance(value, dict)
            and isinstance(data.get(key), dict)
            and "default" not in value
            and "segments" not in value
        ):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return TherapyProfile(data)


def seg(start, end, value):
    return {"start": start, "end": end, "value": value}


class FakeCGM:
    """Deterministic CGM: readings are set explicitly; newest reading timestamped 'now'."""

    def __init__(self, clock):
        self.clock = clock
        self.history = []
        self.trend = "FLAT"
        self.raise_errors = False
        self.arrows = {name: rate for name, rate, _ in TREND_ARROWS}
        self.sensor = {"min_mg_dl": 40, "max_mg_dl": 400}

    def set(self, values, trend="FLAT", spacing_min=5, age_min=0):
        now = self.clock.now() - age_min * MIN
        n = len(values)
        self.history = [
            GlucoseReading(now - spacing_min * MIN * (n - 1 - i), v, trend)
            for i, v in enumerate(values)
        ]
        self.trend = trend

    def _check(self):
        if self.raise_errors:
            raise OSError("lost")

    def get_current_glucose(self):
        self._check()
        return self.history[-1] if self.history else None

    def get_glucose_history(self, minutes=180):
        self._check()
        return list(self.history)

    def get_glucose_trend(self):
        self._check()
        return self.trend

    def get_trend_arrows(self):
        self._check()
        return dict(self.arrows)

    def get_sensor_info(self):
        self._check()
        return dict(self.sensor)


def make_pump(
    profile=None,
    values=(110, 110, 110),
    trend="FLAT",
    reservoir_mu=200000,
    **pump_kwargs
):
    clock = SimulatedClock(START)
    cgm = FakeCGM(clock)
    cgm.set(list(values), trend)
    pump = MockInsulinDelivery(clock, reservoir_mu=reservoir_mu, **pump_kwargs)
    controller = InsulinPumpController(cgm, pump, clock, profile or make_profile())
    return controller, cgm, pump, clock


def run_cycles(controller, cgm, clock, values, n, trend="FLAT"):
    decisions = []
    for _ in range(n):
        cgm.set(values, trend)
        decisions.append(controller.run_cycle())
        clock.advance(5)
    return decisions
