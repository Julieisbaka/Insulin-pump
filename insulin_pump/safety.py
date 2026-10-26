"""Independent dose safety gate and delivery ledger (integer milliunits only).

Every insulin request, whether automatic or manual, must pass SafetyMonitor.authorize()
immediately before reaching the pump hardware. It re-checks the profile limits *and*
the pump's own hardware limits, so a bug in the dosing logic cannot by itself cause
an overdose. Anything unexpected (wrong type, negative, missing profile) is refused:
the default answer is "no insulin".
"""

from .exact import Q, floor_to_increment, format_units, is_int
from .models import DoseKind

HOUR_S = 3600
DAY_S = 86400


class DeliveryLedger:
    """Insulin the pump confirmed (or must assume) it delivered. Old entries are pruned to bound memory."""

    def __init__(self):
        self._doses = []
        # Incremented whenever bolus/correction insulin is recorded; used to invalidate stale bolus advice.
        self.non_basal_version = 0
        self.totals_mu = {kind: 0 for kind in DoseKind.ALL}

    @property
    def doses(self):
        return tuple(self._doses)

    def record(self, dose):
        if dose.mu > 0:
            self._doses.append(dose)
            self.totals_mu[dose.kind] = self.totals_mu.get(dose.kind, 0) + dose.mu
            if dose.kind != DoseKind.BASAL:
                self.non_basal_version += 1

    def prune(self, now, keep_s):
        """Forget doses older than keep_s (must cover the 24 h limit window and the insulin curve)."""
        cutoff = now - keep_s
        self._doses = [d for d in self._doses if d.timestamp > cutoff]

    def total_since(self, now, window_s):
        start = now - window_s
        # Doses timestamped after `now` (clock moved backwards) are counted, to be conservative.
        return sum(d.mu for d in self._doses if d.timestamp > start)

    def insulin_on_board(self, now, curve):
        """Non-basal insulin still active, in mU, rounded UP.

        Scheduled basal is assumed to balance the body's own glucose output, so like
        most pump bolus calculators only bolus and automatic-correction insulin count.
        """
        total = 0
        for dose in self._doses:
            if dose.kind != DoseKind.BASAL:
                total += curve.iob_mu(dose.mu, now - dose.timestamp)
        return total


class SafetyVerdict:
    __slots__ = ("requested_mu", "approved_mu", "reasons")

    def __init__(self, requested_mu, approved_mu, reasons=()):
        self.requested_mu = requested_mu
        self.approved_mu = approved_mu
        self.reasons = tuple(reasons)

    @property
    def limited(self):
        return bool(self.reasons)


class SafetyMonitor:
    def __init__(self, ledger, pump_info):
        self._ledger = ledger
        self._pump = pump_info

    def authorize(self, requested, kind, profile, now, cycle_s):
        """Return how much of `requested` (int mU, or exact Q mU) may be delivered right now."""
        if not (is_int(requested) or isinstance(requested, Q)):
            return SafetyVerdict(0, 0, ("invalid insulin amount requested",))
        if requested < 0:
            return SafetyVerdict(requested, 0, ("negative insulin amount requested",))
        if kind not in DoseKind.ALL:
            return SafetyVerdict(requested, 0, ("unknown dose kind",))
        if profile is None:
            return SafetyVerdict(requested, 0, ("no valid therapy profile",))
        if not is_int(cycle_s) or cycle_s <= 0:
            return SafetyVerdict(requested, 0, ("invalid control cycle length",))

        limits, pump = profile.limits, self._pump
        hourly_used = self._ledger.total_since(now, HOUR_S)
        daily_used = self._ledger.total_since(now, DAY_S)
        caps = [(pump.max_single_delivery_mu, "pump max single delivery")]
        if kind == DoseKind.BOLUS:
            caps.append(
                (
                    limits.max_bolus_mu,
                    "max bolus (%s U)" % format_units(limits.max_bolus_mu),
                )
            )
        elif kind == DoseKind.BASAL:
            caps.append(
                (
                    Q(limits.max_basal_rate_mu_per_hour * cycle_s, HOUR_S).floor(),
                    "max basal rate",
                )
            )
        else:
            caps.append(
                (
                    profile.automation.max_correction_per_cycle_mu,
                    "max automatic correction per cycle",
                )
            )
        hourly_limit = min(limits.max_hourly_mu, pump.max_hourly_mu)
        daily_limit = min(limits.max_daily_mu, pump.max_daily_mu)
        caps.append(
            (
                hourly_limit - hourly_used,
                "max hourly insulin (%s U, %s U used)"
                % (format_units(hourly_limit), format_units(hourly_used)),
            )
        )
        caps.append(
            (
                daily_limit - daily_used,
                "max daily insulin (%s U, %s U used)"
                % (format_units(daily_limit), format_units(daily_used)),
            )
        )
        if kind != DoseKind.BASAL:
            iob = self._ledger.insulin_on_board(now, profile.curve)
            caps.append(
                (
                    limits.max_iob_mu - iob,
                    "max insulin on board (%s U, %s U active)"
                    % (format_units(limits.max_iob_mu), format_units(iob)),
                )
            )

        approved = floor_to_increment(requested, pump.increment_mu)
        reasons = []
        for cap, label in caps:
            if cap < approved:
                approved = floor_to_increment(cap, pump.increment_mu)
                reasons.append("limited by " + label)
        return SafetyVerdict(requested, approved, reasons)


def ledger_retention_s(profile):
    """How long doses must be remembered: the 24 h limit window or the insulin curve, whichever is longer."""
    return max(DAY_S, profile.curve.end_min * 60) + HOUR_S
