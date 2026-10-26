"""Console simulation of the insulin pump (no UI). SIMULATION ONLY - not for medical use.

CPython:
    python -m insulin_pump.simulate [--profile profiles/example_profile.json] [--hours 24] [--seed 42] [--no-faults] [--quiet]
MicroPython (e.g. Raspberry Pi Pico REPL):
    >>> from insulin_pump import simulate
    >>> simulate.run(hours=2)
"""

from .clock import SimulatedClock
from .controller import InsulinPumpController
from .exact import format_units
from .mocks import MockCGM, MockInsulinDelivery
from .models import DoseKind, Severity
from .profile import load_profile
from .schedule import format_clock


def _default_profile_path():
    try:
        path = __file__
    except NameError:
        return "profiles/example_profile.json"
    cut = max(path.rfind("/"), path.rfind("\\"))
    package_dir = path[:cut] if cut >= 0 else "."
    return package_dir + "/../profiles/example_profile.json"


SIM_START_S = 6 * 3600  # day 0, 06:00 local time
# "HH:MM" -> (carbs in grams, whether the user boluses for it)
MEALS = {
    "07:00": (45, True),
    "12:00": (60, True),
    "15:30": (20, False),
    "18:30": (70, True),
}
# Patient physiology used by the mock CGM (the "true" body, unknown to the pump).
PATIENT = {"starting_glucose": 150.0, "insulin_sensitivity": 50.0, "carb_ratio": 10.0}


def run(
    profile=None, hours=24, seed=42, faults=True, verbose=True, summary=None, out=print
):
    profile = profile or load_profile(_default_profile_path())
    clock = SimulatedClock(SIM_START_S)
    cgm = MockCGM(
        clock, basal_need_mu_per_hour=profile.basal_rate.value_at, seed=seed, **PATIENT
    )
    cgm.prime_basal()
    pump = MockInsulinDelivery(clock, reservoir_mu=200000, cgm=cgm)
    controller = InsulinPumpController(cgm, pump, clock, profile)
    say = out if verbose else (lambda *_a: None)
    stats = {"readings": [], "modes": {}, "critical": 0}

    say(profile.describe())
    say(
        "\n{:>5} {:>4} {:>15} {:>4} {:>5} {:>5}  mode / alerts".format(
            "time", "BG", "trend", "pred", "IOB", "dose"
        )
    )
    cycle_minutes = profile.loop.cycle_minutes
    for _ in range(hours * 60 // cycle_minutes):
        hhmm = format_clock(clock.now())
        if faults:
            _inject_faults(hhmm, cgm, pump, controller, say)
        if hhmm in MEALS:
            carbs, bolus = MEALS[hhmm]
            cgm.add_meal(carbs)
            say(
                "%s  >> meal %d g%s"
                % (hhmm, carbs, "" if bolus else " (no bolus given)")
            )
            if bolus:
                rec = controller.calculate_bolus(carbs)
                if rec.blocked:
                    say("        bolus blocked: %s" % rec.blocked_reason)
                else:
                    result = controller.confirm_bolus(rec.id)
                    say("        bolus: %s -> %s" % (rec.summary(), result.message))
                for w in rec.warnings:
                    say("        warning: %s" % w)

        d = controller.run_cycle()
        _record(stats, d)
        say(_format(d))
        clock.advance(cycle_minutes)
        cgm.step(cycle_minutes)

    if verbose if summary is None else summary:
        _summary(controller, pump, stats, out)
    return controller


def _inject_faults(hhmm, cgm, pump, controller, say):
    if hhmm == "10:00":
        cgm.dropout = True
        say("%s  !! injecting CGM signal loss" % hhmm)
    elif hhmm == "10:45":
        cgm.dropout = False
        say("%s  !! CGM signal restored" % hhmm)
    elif hhmm == "14:00":
        pump.occluded = True
        say("%s  !! injecting infusion-set occlusion" % hhmm)
    elif hhmm == "14:20":
        pump.occluded = False
        controller.clear_fault()
        say("%s  >> user replaced infusion set and cleared the fault" % hhmm)


def _record(stats, d):
    if d.glucose is not None:
        stats["readings"].append(d.glucose)
    stats["modes"][d.mode] = stats["modes"].get(d.mode, 0) + 1
    stats["critical"] += sum(1 for a in d.alerts if a.severity == Severity.CRITICAL)


def _format(d):
    bg = "{:>4}".format(d.glucose if d.glucose is not None else "-")
    pred = "{:>4}".format(
        d.predicted_glucose if d.predicted_glucose is not None else "-"
    )
    trend = "{:>15}".format(d.trend or "-")
    alerts = "; ".join("[%s] %s" % (a.severity.upper(), a.message) for a in d.alerts)
    line = "%s %s %s %s %s %s  %s" % (
        format_clock(d.timestamp),
        bg,
        trend,
        pred,
        "{:>5}".format(format_units(d.iob_mu)),
        "{:>5}".format(format_units(d.delivered_mu)),
        d.mode,
    )
    return line + (" | " + alerts if alerts else "")


def _summary(controller, pump, stats, say):
    totals = controller.ledger.totals_mu
    readings = stats["readings"]
    say("\n=== Summary ===")
    say(
        "Insulin: "
        + ", ".join(
            "%s %s U" % (k, format_units(totals.get(k, 0))) for k in DoseKind.ALL
        )
        + " | total %s U | reservoir %s U"
        % (format_units(sum(totals.values())), format_units(pump.reservoir_mu))
    )
    if readings:
        in_range = sum(1 for g in readings if 70 <= g <= 180) * 100 // len(readings)
        say(
            "CGM min/max: %d/%d mg/dL | time in range 70-180: %d%%"
            % (min(readings), max(readings), in_range)
        )
    modes = ", ".join("%s: %d" % (k, stats["modes"][k]) for k in sorted(stats["modes"]))
    say("Cycles by mode: {%s} | critical alerts: %d" % (modes, stats["critical"]))
    say("Audit log:")
    for event in controller.audit_log:
        if event.event not in ("bolus_calculated", "cgm_info"):
            say(
                "  %s %s: %s"
                % (
                    format_clock(event.timestamp),
                    event.event,
                    event.details.split("\n")[0],
                )
            )


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Insulin pump simulation (mock CGM + mock pump)"
    )
    parser.add_argument(
        "--profile", default=_default_profile_path(), help="therapy profile JSON file"
    )
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--no-faults",
        action="store_true",
        help="skip CGM dropout / occlusion injection",
    )
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()
    run(
        load_profile(args.profile),
        args.hours,
        args.seed,
        not args.no_faults,
        verbose=not args.quiet,
        summary=True,
    )


if __name__ == "__main__":
    main()
