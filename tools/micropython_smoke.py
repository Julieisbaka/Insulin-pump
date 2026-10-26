"""On-device smoke test for MicroPython (Raspberry Pi Pico) - also runs on CPython.

Copy the package, schema and example profile to the board, then:
    mpremote run tools/micropython_smoke.py
It checks the exact maths and key safety behaviour, then runs a short simulation.
"""

import sys

from insulin_pump.clock import SimulatedClock
from insulin_pump.controller import InsulinPumpController
from insulin_pump.exact import Q, parse_decimal
from insulin_pump.insulin_curve import PPB, InsulinCurve
from insulin_pump.mocks import MockCGM, MockInsulinDelivery
from insulin_pump.models import Mode
from insulin_pump.profile import ProfileValidationError, TherapyProfile, load_profile

PROFILE_PATH = "profiles/example_profile.json"
failures = []


def check(label, condition):
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        failures.append(label)


print("implementation:", sys.implementation.name, sys.version)

check(
    "exact decimal maths",
    parse_decimal("0.1") + parse_decimal("0.2") == parse_decimal("0.3"),
)
check("big integers", 10**40 // 7 == 1428571428571428571428571428571428571428)
curve = InsulinCurve("envelope", 360, 75, 10)
check("insulin curve shape", curve.table()[0] == PPB and curve.table()[-1] == 0)
check("insulin curve value at 60 min", curve.table()[60] == 837837838)

profile = load_profile(PROFILE_PATH)
check("example profile loads", profile.name == "Example adult profile")
check(
    "schema defaults applied",
    profile.cgm.fallback_basal_minutes == 0 and profile.limits.max_bolus_mu == 10000,
)
check("fractions are exact", profile.automation.correction_fraction == Q(3, 10))
try:
    TherapyProfile({"name": "bad"})
    check("invalid profile rejected", False)
except ProfileValidationError as exc:
    check("invalid profile rejected", len(exc.errors) >= 1)

clock = SimulatedClock(12 * 3600)
cgm = MockCGM(clock, basal_need_mu_per_hour=profile.basal_rate.value_at, seed=3)
pump = MockInsulinDelivery(clock, cgm=cgm)
controller = InsulinPumpController(cgm, pump, clock, profile)
check("closed loop runs", controller.run_cycle().mode == Mode.CLOSED_LOOP)
cgm.dropout = True
clock.advance(35)
check("CGM loss withholds insulin", controller.run_cycle().mode == Mode.NO_CGM_SUSPEND)
cgm.dropout = False
cgm.step(0)
pump.occluded = True
clock.advance(5)
check("occlusion latches a fault", controller.run_cycle().mode == Mode.FAULT)

from insulin_pump import simulate

lines = []
simulate.run(profile, hours=24, seed=42, verbose=False, summary=True, out=lines.append)
for line in lines[:4]:
    print("   ", line)
check(
    "24 h simulation completed", any(line.startswith("CGM min/max") for line in lines)
)

print("RESULT:", "PASS" if not failures else "FAIL (%d)" % len(failures))
