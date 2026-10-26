"""Insulin-on-board (IOB) curves, computed with integer arithmetic only.

Three models are supported (selected by the profile's ``insulin.curve``):

``exponential``
    The model used by Loop and OpenAPS (Dragan Maksimovic's formulation,
    LoopKit ``ExponentialInsulinModel`` / oref0 ``iobCalcExponential``)::

        tau = tp * (1 - tp/td) / (1 - 2*tp/td)
        a   = 2 * tau / td
        S   = 1 / (1 - a + (1 + a) * exp(-td/tau))
        IOB(t) = 1 - S*(1-a)*((t^2/(tau*td*(1-a)) - t/tau - 1)*exp(-t/tau) + 1)

    with ``td`` = action duration, ``tp`` = peak time and an optional delay before
    insulin starts acting.

``linear``
    IOB falls in a straight line from 100% to 0% over delay + duration.

``envelope`` (default)
    At every minute the *larger* of the two. Linear under-estimates IOB in the
    first hour (when stacking risk is highest) and exponential is lower than
    linear afterwards, so taking the maximum never under-counts insulin
    relative to either model. Over-estimating IOB only means less insulin.

The curve is tabulated once per profile, one entry per minute, in parts per
billion (ppb) of the dose. Each entry is rounded *up*, and has 1 ppb added to
cover arithmetic rounding. Look-ups use whole elapsed minutes rounded *down*.
Because the curve never increases, both choices can only over-estimate IOB.
"""

from .exact import Q, ceil_div, is_int

PPB = 1000000000
_FIXED = 10**36  # working precision for the exponential (about 1e-36)


class CurveError(ValueError):
    pass


def _exp_neg_fixed(x):
    """floor(exp(-x) * _FIXED) for a rational x >= 0 (Taylor series, integer-only)."""
    if x < 0:
        raise CurveError("negative exponent")
    whole = x.floor()
    frac = x - whole
    result = _exp_neg_unit(frac)
    if whole:
        e_inv = _exp_neg_unit(Q(1))
        for _ in range(whole):
            result = result * e_inv // _FIXED
    return result


def _exp_neg_unit(r):
    """exp(-r) for 0 <= r <= 1 at _FIXED scale. Alternating series; the truncation error is below the last term."""
    guard = _FIXED * 10**6
    total = 0
    term = guard
    k = 0
    while term:
        total = total + term if k % 2 == 0 else total - term
        k += 1
        term = term * r.n // (r.d * k)
    return total // 10**6


class InsulinCurve:
    """Pre-computed, monotonically non-increasing IOB fraction table."""

    MODELS = ("envelope", "exponential", "linear")

    def __init__(self, model, duration_min, peak_min, delay_min):
        for name, value in (
            ("duration_min", duration_min),
            ("peak_min", peak_min),
            ("delay_min", delay_min),
        ):
            if not is_int(value) or value < 0:
                raise CurveError(
                    "%s must be a non-negative whole number of minutes" % name
                )
        if model not in self.MODELS:
            raise CurveError("unknown insulin curve %r" % (model,))
        if not 0 < peak_min or not 2 * peak_min < duration_min:
            raise CurveError(
                "insulin peak time must be greater than 0 and less than half of the action duration"
            )
        self.model = model
        self.duration_min = duration_min
        self.peak_min = peak_min
        self.delay_min = delay_min
        self.end_min = delay_min + duration_min
        self._table = self._build()

    # ---- public -------------------------------------------------------------------

    def remaining_ppb(self, elapsed_s):
        """Fraction of a dose still active, in ppb, after `elapsed_s` seconds (rounded toward MORE insulin)."""
        if elapsed_s <= 0:
            return PPB  # future-dated doses count in full
        minute = elapsed_s // 60
        if minute >= len(self._table):
            return 0
        return self._table[minute]

    def iob_mu(self, dose_mu, elapsed_s):
        return ceil_div(dose_mu * self.remaining_ppb(elapsed_s), PPB)

    def table(self):
        return list(self._table)

    # ---- construction --------------------------------------------------------------

    def _build(self):
        linear = [self._linear_ppb(m) for m in range(self.end_min + 1)]
        if self.model == "linear":
            table = linear
        else:
            exponential = self._exponential_table()
            if self.model == "exponential":
                table = exponential
            else:
                table = [a if a >= b else b for a, b in zip(linear, exponential)]
        self._self_check(table)
        return table

    def _linear_ppb(self, minute):
        if minute >= self.end_min:
            return 0
        return ceil_div((self.end_min - minute) * PPB, self.end_min)

    def _exponential_table(self):
        td, tp = self.duration_min, self.peak_min
        tau = Q(tp * (td - tp), td - 2 * tp)
        a = tau * 2 / td
        one_minus_a = Q(1) - a
        e_end = Q(_exp_neg_fixed(Q(td) / tau), _FIXED)
        s = Q(1) / (one_minus_a + (Q(1) + a) * e_end)
        # Fixed-point constants (floor/ceil errors ~1e-36, far below the 1 ppb safety margin).
        f = _FIXED
        sa = (s * one_minus_a * f).floor()
        k1 = (Q(f) / (tau * td * one_minus_a)).floor()
        k2 = (Q(f) / tau).floor()
        step = _exp_neg_fixed(Q(1) / tau)  # exp(-1/tau)

        table = []
        e_t = f  # exp(-t/tau) at t = 0
        for minute in range(self.end_min + 1):
            t = minute - self.delay_min
            if t <= 0:
                table.append(PPB)
                continue
            if t >= td:
                table.append(0)
                continue
            e_t = e_t * step // f
            inner = t * t * k1 - t * k2 - f
            remaining = f - sa * (inner * e_t // f + f) // f
            ppb = ceil_div(remaining * PPB, f) + 1
            table.append(min(PPB, max(0, ppb)))
        return table

    def _self_check(self, table):
        if not table or table[0] != PPB or table[-1] != 0:
            raise CurveError("insulin curve must start at 100% and end at 0%")
        for prev, cur in zip(table, table[1:]):
            if cur > prev:
                raise CurveError("insulin curve is not monotonically decreasing")
