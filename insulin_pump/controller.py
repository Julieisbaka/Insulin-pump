"""Closed-loop insulin pump controller.

SIMULATION ONLY - this is an educational model and is NOT suitable for
controlling a real medical device.

Guiding rule: high glucose is less immediately dangerous than low glucose,
so the default action is to give NO insulin. Insulin is delivered only when
every check positively passes; any doubt, error or missing data withholds it.

Safety design:
  * Settings come from a schema-validated TherapyProfile; values follow the time-of-day schedules.
  * Dosing maths is exact: integer milliunits, integer mg/dL, integer seconds and
    exact rationals. Every unavoidable rounding goes toward less insulin.
  * CGM data is classified (stale / missing / out-of-range / implausible / no trend)
    before use. Anything that suggests a low is treated as low. Other unreliable
    data withholds insulin, unless the profile allows a limited fallback-basal window.
  * Low and predicted-low suspend is latched. It resumes only when glucose is
    reliably back above the person's low threshold plus a margin and not falling.
  * Every dose passes an independent SafetyMonitor (profile limits + pump hardware
    limits) immediately before reaching the pump.
  * Delivery is verified. Failures, partial deliveries, over-deliveries, driver
    errors, invalid responses, unexpected internal errors and a clock moving
    backwards latch a FAULT that stops all insulin until the user clears it. If the
    outcome is unknown, the full amount is assumed delivered so insulin on board
    is never underestimated.
  * Missed control cycles are never "caught up"; early/duplicate cycles deliver nothing.
  * Boluses use calculate -> confirm. Recommendations are single-use, expire, and are
    invalidated by any later bolus or correction or by any change in scheduled values.
    Manual amounts above a limit are refused rather than silently reduced.
  * Logs are bounded so memory use stays fixed on a microcontroller.
"""

from .exact import (
    MU_PER_UNIT,
    Q,
    clamp,
    floor_to_increment,
    format_decimal,
    format_units,
    is_int,
)
from .glucose import assess_glucose
from .models import (
    AuditEvent,
    BolusRecommendation,
    BolusResult,
    CycleDecision,
    DeliveryResult,
    Dose,
    DoseKind,
    Mode,
    PumpInfo,
    SensorInfo,
    SensorStatus,
    Severity,
)
from .profile import ProfileValidationError, TherapyProfile
from .safety import DeliveryLedger, SafetyMonitor, ledger_retention_s


def _fmt(value):
    return "-" if value is None else str(value)


def _bounded_append(items, item, limit):
    items.append(item)
    while len(items) > limit:
        items.pop(0)


class _PendingBolus:
    __slots__ = ("rec", "ledger_version", "snapshot", "meter")

    def __init__(self, rec, ledger_version, snapshot, meter):
        self.rec = rec
        self.ledger_version = ledger_version
        self.snapshot = snapshot
        self.meter = meter


class InsulinPumpController:
    """`cgm` must provide get_current_glucose(), get_glucose_history(minutes), get_glucose_trend(),
    get_sensor_info() and get_trend_arrows(). `pump` must provide get_pump_info(),
    get_reservoir_mu() and give_insulin(mu) -> DeliveryResult. `clock.now()` returns
    integer local seconds."""

    def __init__(self, cgm, pump, clock, profile):
        if not isinstance(profile, TherapyProfile):
            raise TypeError("profile must be a validated TherapyProfile")
        self.cgm = cgm
        self.pump = pump
        self.clock = clock
        self.pump_info = PumpInfo(pump.get_pump_info())
        errors = profile.device_errors(self.pump_info)
        if errors:
            raise ProfileValidationError(errors)
        self.ledger = DeliveryLedger()
        self.safety = SafetyMonitor(self.ledger, self.pump_info)
        self.log = []
        self.audit_log = []
        self.last_exception = None
        self._profile = profile
        self._sensor = None
        self._fault = None
        self._user_suspended = False
        self._user_suspend_until = None
        self._low_suspend_since = None
        self._no_cgm_since = None
        self._last_reliable_time = None
        self._last_cycle = None
        self._last_seen_time = None
        self._basal_carry = Q(0)
        self._pending_bolus = None
        self._next_recommendation_id = 1
        self._audit(
            self._now(), "startup", "Pump started with profile '%s'" % profile.name
        )

    # ---- State ---------------------------------------------------------------------

    @property
    def profile(self):
        return self._profile

    @property
    def fault(self):
        return self._fault

    @property
    def low_suspended(self):
        return self._low_suspend_since is not None

    @property
    def user_suspended(self):
        return self._user_suspended

    @property
    def cycle_s(self):
        return self._profile.loop.cycle_minutes * 60

    def insulin_on_board_mu(self):
        return self.ledger.insulin_on_board(self.clock.now(), self._profile.curve)

    # ---- User actions --------------------------------------------------------------

    def update_profile(self, profile):
        if not isinstance(profile, TherapyProfile):
            raise TypeError("profile must be a validated TherapyProfile")
        errors = profile.device_errors(self.pump_info)
        if errors:
            raise ProfileValidationError(errors)
        now = self._now()
        old = self._profile
        self._profile = profile
        self._pending_bolus = None
        self._basal_carry = Q(0)
        self._audit(now, "profile_changed", "'%s' -> '%s'" % (old.name, profile.name))

    def suspend(self, minutes=None):
        now = self._now()
        if minutes is not None:
            minutes = _validated_int(minutes, "minutes", 1, self._profile.loop.max_timed_suspend_minutes)
        self._user_suspended = True
        self._user_suspend_until = None if minutes is None else now + minutes * 60
        self._pending_bolus = None
        self._audit(
            now, "user_suspend", "for %d min" % minutes if minutes else "until resumed"
        )

    def resume(self):
        """User resume. Also clears a latched low suspend; it re-engages next cycle if still low."""
        now = self._now()
        self._user_suspended = False
        self._user_suspend_until = None
        if self._low_suspend_since is not None:
            self._audit(
                now,
                "low_suspend_user_resume",
                "User resumed insulin during low suspend",
            )
        self._low_suspend_since = None
        self._audit(now, "user_resume", "Insulin delivery resumed by user")

    def clear_fault(self):
        """Acknowledge and clear a latched fault after the user has resolved its cause."""
        if self._fault is None:
            return False
        now = self.clock.now()
        self._audit(now, "fault_cleared", "User cleared fault: %s" % self._fault)
        self._fault = None
        self._last_seen_time = now
        self._basal_carry = Q(0)
        self._pending_bolus = None
        return True

    # ---- Control loop --------------------------------------------------------------

    def run_cycle(self):
        now = self._now()
        try:
            return self._run_cycle(now)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - any unexpected error must stop insulin
            self.last_exception = exc
            self._latch_fault(now, "internal controller error: %r" % (exc,))
            d = CycleDecision(now, SensorStatus.NO_DATA, None, None, None, 0, Q(0))
            d.mode = Mode.FAULT
            d.alert(
                Severity.CRITICAL,
                "fault",
                "PUMP FAULT - insulin stopped: %s" % self._fault,
            )
            _bounded_append(self.log, d, self._profile.logging.max_decisions)
            return d

    def _run_cycle(self, now):
        profile = self._profile
        snap = profile.at(now)
        a = self._assess(now)
        if a.reliable:
            self._last_reliable_time = now
        iob = self.ledger.insulin_on_board(now, profile.curve)
        scheduled = self._scheduled_basal(now)
        d = CycleDecision(
            now,
            a.status,
            a.glucose,
            a.reading.trend if a.reading else None,
            a.predicted,
            iob,
            scheduled,
        )

        # A duplicate or early call (scheduler retry, double callback) must never deliver basal twice.
        too_soon = (
            self._last_cycle is not None
            and (now - self._last_cycle) * 100
            < self.cycle_s * profile.loop.min_cycle_spacing_percent
        )
        if not too_soon:
            self._check_loop_timing(now, d)
        self._glucose_alarms(a, d)
        self._expire_user_suspend(now, d)

        if self._fault is not None:
            d.mode = Mode.FAULT
        elif self._user_suspended:
            d.mode = Mode.USER_SUSPEND
            d.alert(
                Severity.WARNING, "user_suspend", "Insulin delivery suspended by user"
            )
        elif self._low_suspend_active(a, snap, now, d):
            d.mode = Mode.LOW_SUSPEND
        elif not a.reliable:
            if self._fallback_allowed(now):
                d.mode = Mode.FALLBACK_BASAL
                d.basal_request = scheduled
                d.alert(
                    Severity.WARNING,
                    "fallback",
                    "No reliable CGM data - limited scheduled basal only, automatic corrections off",
                )
            else:
                d.mode = Mode.NO_CGM_SUSPEND
                d.alert(
                    Severity.WARNING,
                    "no_cgm_suspend",
                    "No reliable CGM data - insulin withheld. Check glucose with a meter.",
                )
        else:
            d.basal_request, d.correction_mu = self._closed_loop_request(
                a, snap, iob, scheduled
            )
        self._track_cgm_withholding(now, d)

        if too_soon:
            d.basal_request = 0
            d.correction_mu = 0
            d.alert(
                Severity.INFO,
                "cycle_too_soon",
                "Control cycle requested early - no insulin delivered this call",
            )
        elif d.mode in Mode.DELIVERING:
            self._deliver_cycle(d, now)
        else:
            self._basal_carry = Q(0)

        if self._fault is not None:
            d.mode = Mode.FAULT
            d.alert(
                Severity.CRITICAL,
                "fault",
                "PUMP FAULT - insulin stopped: %s. Resolve, then clear the fault."
                % self._fault,
            )
        self._reservoir_alerts(d)
        if not too_soon:
            self._last_cycle = now
        self.ledger.prune(now, ledger_retention_s(profile))
        _bounded_append(self.log, d, profile.logging.max_decisions)
        return d

    def _scheduled_basal(self, start):
        """Exact scheduled basal (Q mU) for the coming cycle; rate changes inside the cycle are prorated."""
        numerator = 0  # mU/hr * seconds
        t, end = start, start + self.cycle_s
        basal = self._profile.basal_rate
        while t < end:
            chunk_end = min(end, (t // 60 + 1) * 60)
            numerator += basal.value_at(t) * (chunk_end - t)
            t = chunk_end
        return Q(numerator, 3600)

    def _fallback_allowed(self, now):
        window_s = self._profile.cgm.fallback_basal_minutes * 60
        if window_s <= 0 or self._last_reliable_time is None:
            return False
        return now - self._last_reliable_time < window_s

    def _track_cgm_withholding(self, now, d):
        if d.mode == Mode.NO_CGM_SUSPEND:
            if self._no_cgm_since is None:
                self._no_cgm_since = now
                self._audit(
                    now, "no_cgm_suspend", "Insulin withheld: %s" % d.sensor_status
                )
            minutes = (now - self._no_cgm_since) // 60
            if minutes >= self._profile.alarms.no_insulin_critical_minutes:
                d.alert(
                    Severity.CRITICAL,
                    "no_insulin",
                    "No insulin for %d min because CGM data is unavailable. Check glucose and ketones; "
                    "fix the CGM or take manual action." % minutes,
                )
        elif self._no_cgm_since is not None and d.mode != Mode.FAULT:
            self._audit(
                now,
                "no_cgm_suspend_end",
                "CGM-related insulin hold ended after %d min"
                % ((now - self._no_cgm_since) // 60),
            )
            self._no_cgm_since = None

    def _closed_loop_request(self, a, snap, iob, scheduled):
        basal = scheduled
        if a.predicted < snap.target:
            # Reduce basal linearly to zero as the prediction approaches the low threshold.
            factor = clamp(
                Q(a.predicted - snap.low_threshold, snap.target - snap.low_threshold),
                0,
                1,
            )
            basal = scheduled * factor
        correction = 0
        auto = self._profile.automation
        if (
            auto.enabled
            and a.glucose > snap.correct_above
            and a.predicted > snap.correct_above
        ):
            # Size the correction on the lower of current and predicted glucose (less insulin when they disagree).
            basis = a.glucose if a.glucose < a.predicted else a.predicted
            needed = (
                Q((basis - snap.target) * MU_PER_UNIT) / snap.correction_factor
            ).floor() - iob
            if needed > 0:
                correction = min(
                    (auto.correction_fraction * needed).floor(),
                    auto.max_correction_per_cycle_mu,
                )
        return basal, correction

    def _deliver_cycle(self, d, now):
        profile = self._profile
        request = self._basal_carry + d.basal_request
        verdict = self.safety.authorize(
            request, DoseKind.BASAL, profile, now, self.cycle_s
        )
        # Sub-increment remainders carry (exactly) to the next cycle so basal isn't systematically under-delivered.
        self._basal_carry = Q(0) if verdict.limited else request - verdict.approved_mu
        for reason in verdict.reasons:
            d.alert(Severity.WARNING, "limit", "Basal " + reason)
        if verdict.approved_mu > 0:
            d.delivered_mu += self._deliver(verdict.approved_mu, DoseKind.BASAL, now)

        if self._fault is None and d.correction_mu > 0:
            verdict = self.safety.authorize(
                d.correction_mu, DoseKind.AUTO_CORRECTION, profile, now, self.cycle_s
            )
            for reason in verdict.reasons:
                d.alert(Severity.INFO, "limit", "Automatic correction " + reason)
            if verdict.approved_mu > 0:
                d.delivered_mu += self._deliver(
                    verdict.approved_mu, DoseKind.AUTO_CORRECTION, now
                )

    def _low_suspend_active(self, a, snap, now, d):
        low = snap.low_threshold
        if a.indicates_low(low):
            if self._low_suspend_since is None:
                self._low_suspend_since = now
                self._audit(
                    now,
                    "low_suspend",
                    "glucose=%s predicted=%s status=%s low threshold=%d"
                    % (_fmt(a.glucose), _fmt(a.predicted), a.status, low),
                )
            if a.is_low(low):
                d.alert(
                    Severity.CRITICAL,
                    "low",
                    "Glucose at or below your low threshold (%d mg/dL) - insulin suspended. Treat the low."
                    % low,
                )
            else:
                d.alert(
                    Severity.WARNING,
                    "predicted_low",
                    "Glucose predicted below %d mg/dL within %d min - insulin suspended"
                    % (low, self._profile.cgm.prediction_horizon_minutes),
                )
            self._long_suspend_alarm(now, d)
            return True

        if self._low_suspend_since is None:
            return False

        settings = self._profile.low_suspend
        resume_level = low + settings.resume_margin_mg_dl
        recovered = (
            a.reliable
            and a.glucose >= resume_level
            and a.predicted >= resume_level
            and a.rate > settings.resume_min_rate
        )
        if recovered:
            minutes = (now - self._low_suspend_since) // 60
            self._low_suspend_since = None
            self._audit(
                now,
                "low_resume",
                "Resumed after %d min at %d mg/dL" % (minutes, a.glucose),
            )
            d.alert(
                Severity.INFO,
                "low_resume",
                "Glucose recovered to %d mg/dL - insulin resumed after %d min"
                % (a.glucose, minutes),
            )
            return False

        d.alert(
            Severity.WARNING,
            "low_suspend_hold",
            "Insulin stays suspended until glucose is reliably above %d mg/dL and not falling"
            % resume_level,
        )
        self._long_suspend_alarm(now, d)
        return True

    def _long_suspend_alarm(self, now, d):
        minutes = (now - self._low_suspend_since) // 60
        if minutes >= self._profile.alarms.long_low_suspend_minutes:
            d.alert(
                Severity.CRITICAL,
                "long_suspend",
                "Insulin suspended for %d min. Check glucose with a meter; resume manually if appropriate."
                % minutes,
            )

    def _glucose_alarms(self, a, d):
        alarms = self._profile.alarms
        if a.status == SensorStatus.SENSOR_LOW:
            d.alert(
                Severity.CRITICAL,
                "urgent_low",
                "CGM reads LOW (below its range) - treat immediately",
            )
        elif a.status == SensorStatus.SENSOR_HIGH:
            d.alert(
                Severity.WARNING,
                "sensor_high",
                "CGM reads HIGH (above its range) - check with a meter",
            )
        elif not a.reliable:
            d.alert(Severity.WARNING, "sensor", "CGM problem: %s" % a.message)
        if a.glucose is not None and a.status in (
            SensorStatus.OK,
            SensorStatus.IMPLAUSIBLE,
            SensorStatus.NO_TREND,
        ):
            if a.glucose <= alarms.urgent_low:
                d.alert(
                    Severity.CRITICAL,
                    "urgent_low",
                    "URGENT LOW: %d mg/dL - treat immediately" % a.glucose,
                )
            elif a.glucose >= alarms.high_alert:
                d.alert(Severity.WARNING, "high", "High glucose: %d mg/dL" % a.glucose)

    def _check_loop_timing(self, now, d):
        if self._last_cycle is None:
            return
        gap = now - self._last_cycle
        if gap > self._profile.loop.missed_cycle_alert_multiple * self.cycle_s:
            missed = gap // self.cycle_s - 1
            d.alert(
                Severity.WARNING,
                "missed_cycles",
                "Control loop missed ~%d cycle(s); missed basal is not made up"
                % missed,
            )
            self._basal_carry = Q(0)

    def _expire_user_suspend(self, now, d):
        if (
            self._user_suspended
            and self._user_suspend_until is not None
            and now >= self._user_suspend_until
        ):
            self._user_suspended = False
            self._user_suspend_until = None
            self._audit(now, "user_resume", "Timed suspend ended")
            d.alert(
                Severity.INFO,
                "suspend_ended",
                "Timed suspend ended - insulin delivery resumed",
            )

    def _reservoir_mu(self):
        try:
            value = self.pump.get_reservoir_mu()
        except Exception:  # noqa: BLE001
            return None
        return value if is_int(value) and value >= 0 else None

    def _reservoir_alerts(self, d):
        reservoir = self._reservoir_mu()
        alarms = self._profile.alarms
        if reservoir is None:
            d.alert(Severity.WARNING, "reservoir", "Reservoir level unknown")
        elif reservoir < alarms.low_reservoir_mu:
            severity = (
                Severity.CRITICAL
                if reservoir < alarms.critical_reservoir_mu
                else Severity.WARNING
            )
            d.alert(
                severity,
                "reservoir",
                "Low reservoir: %s U remaining" % format_units(reservoir, 1),
            )

    # ---- Bolus -----------------------------------------------------------------------

    def calculate_bolus(self, carbs_g, meter_glucose=None):
        profile = self._profile
        carbs = _validated_int(carbs_g, "carbs_g", 0, profile.limits.max_carbs_grams)
        meter = None
        if meter_glucose is not None:
            meter = _validated_int(
                meter_glucose,
                "meter_glucose",
                profile.bolus.meter_min_mg_dl,
                profile.bolus.meter_max_mg_dl,
            )
        now = self._now()
        snap = profile.at(now)
        a = self._assess(now)
        iob = self.ledger.insulin_on_board(now, profile.curve)
        increment = self.pump_info.increment_mu
        warnings = []
        blocked = self._bolus_block_reason() or self._bolus_glucose_block(
            a, meter, snap
        )

        if meter is not None:
            glucose, source = meter, "meter"
        elif a.reliable:
            glucose, source = a.glucose, "cgm"
        else:
            glucose, source = None, "none"
            if profile.bolus.require_glucose:
                blocked = blocked or (
                    "No reliable CGM reading (%s) - enter a meter reading to bolus"
                    % a.message
                )
            else:
                warnings.append(
                    "No reliable CGM reading (%s); no correction included." % a.message
                )

        carb_mu = (Q(carbs * MU_PER_UNIT) / snap.carb_ratio).floor()
        correction = 0
        if glucose is not None:
            delta = Q((glucose - snap.target) * MU_PER_UNIT) / snap.correction_factor
            if glucose > snap.correct_above:
                # Insulin on board only offsets the correction, never the carb dose.
                correction = max(0, delta.floor() - iob)
            elif glucose < snap.target:
                correction = (
                    delta.floor()
                )  # negative: reduces the meal bolus (rounded toward less insulin)
        recommended = floor_to_increment(carb_mu + correction, increment)
        max_bolus = floor_to_increment(profile.limits.max_bolus_mu, increment)
        if recommended > max_bolus:
            warnings.append(
                "Calculated %s U exceeds your max bolus (%s U); recommendation capped"
                % (format_units(recommended), format_units(profile.limits.max_bolus_mu))
            )
            recommended = max_bolus
        if blocked:
            recommended = 0

        rec = BolusRecommendation(
            id=self._next_recommendation_id,
            created_at=now,
            carbs_g=carbs,
            glucose=glucose,
            glucose_source=source,
            carb_ratio=snap.carb_ratio,
            correction_factor=snap.correction_factor,
            target=snap.target,
            carb_mu=carb_mu,
            correction_mu=correction,
            iob_mu=iob,
            recommended_mu=recommended,
            warnings=warnings,
            blocked_reason=blocked,
        )
        self._next_recommendation_id += 1
        self._pending_bolus = (
            None
            if blocked
            else _PendingBolus(rec, self.ledger.non_basal_version, snap, meter)
        )
        self._audit(
            now,
            "bolus_calculated",
            "carbs=%dg glucose=%s (%s) iob=%s -> %s U%s"
            % (
                carbs,
                _fmt(glucose),
                source,
                format_units(iob),
                format_units(recommended),
                (" BLOCKED: " + blocked) if blocked else "",
            ),
        )
        return rec

    def confirm_bolus(self, recommendation_id, override_mu=None):
        now = self._now()
        pending = self._pending_bolus
        if pending is None or pending.rec.id != recommendation_id:
            return self._bolus_refused(
                now, "Unknown, blocked or already-used recommendation - recalculate"
            )
        self._pending_bolus = None  # single use
        rec = pending.rec
        profile = self._profile

        if now - rec.created_at > profile.bolus.recommendation_ttl_minutes * 60:
            return self._bolus_refused(now, "Recommendation expired - recalculate")
        if self.ledger.non_basal_version != pending.ledger_version:
            return self._bolus_refused(
                now,
                "Bolus or correction insulin was delivered after this was calculated - recalculate",
            )
        snap = profile.at(now)
        if snap != pending.snapshot:
            return self._bolus_refused(
                now,
                "Scheduled settings changed since this was calculated - recalculate",
            )
        blocked = self._bolus_block_reason() or self._bolus_glucose_block(
            self._assess(now), pending.meter, snap
        )
        if blocked:
            return self._bolus_refused(now, blocked)

        if override_mu is None:
            amount = rec.recommended_mu
        else:
            try:
                amount = _validated_int(
                    override_mu, "override_mu", 0, self.pump_info.max_single_delivery_mu
                )
            except ValueError as exc:
                return self._bolus_refused(now, str(exc))
            if amount % self.pump_info.increment_mu:
                return self._bolus_refused(
                    now,
                    "Bolus must be a multiple of %s U"
                    % format_units(self.pump_info.increment_mu),
                )
        if amount <= 0:
            return self._bolus_refused(now, "Bolus amount is 0 U")

        verdict = self.safety.authorize(
            amount, DoseKind.BOLUS, profile, now, self.cycle_s
        )
        if verdict.limited or verdict.approved_mu != amount:
            return self._bolus_refused(
                now,
                "Bolus of %s U refused: %s"
                % (format_units(amount), "; ".join(verdict.reasons)),
            )
        reservoir = self._reservoir_mu()
        if reservoir is None or amount > reservoir:
            return self._bolus_refused(
                now, "Not enough insulin in reservoir (or level unknown)"
            )

        if override_mu is not None and amount != rec.recommended_mu:
            self._audit(
                now,
                "bolus_override",
                "User changed %s U -> %s U"
                % (format_units(rec.recommended_mu), format_units(amount)),
            )
        delivered = self._deliver(amount, DoseKind.BOLUS, now)
        if self._fault is not None:
            return BolusResult(False, delivered, "Bolus problem - %s" % self._fault)
        self._audit(now, "bolus_delivered", "%s U" % format_units(delivered))
        return BolusResult(True, delivered, "Delivered %s U" % format_units(delivered))

    def _bolus_block_reason(self):
        if self._fault is not None:
            return "Pump fault: %s" % self._fault
        if self._user_suspended:
            return "Pump is suspended - resume before bolusing"
        return None

    def _bolus_glucose_block(self, a, meter, snap):
        """Refuse a bolus whenever any evidence points to a current or imminent low."""
        low = snap.low_threshold
        if meter is not None and meter <= low:
            return (
                "Meter glucose %d mg/dL is at or below your low threshold (%d) - treat the low first"
                % (meter, low)
            )
        if a.status == SensorStatus.SENSOR_LOW:
            return "CGM reads LOW - treat the low before bolusing"
        if a.status in (
            SensorStatus.OK,
            SensorStatus.IMPLAUSIBLE,
            SensorStatus.NO_TREND,
        ):
            if a.is_low(low):
                return (
                    "CGM glucose %d mg/dL is at or below your low threshold (%d) - treat the low first"
                    % (a.glucose, low)
                )
            if a.indicates_low(low):
                return (
                    "CGM predicts glucose below %d mg/dL within %d min - wait until glucose stabilises"
                    % (low, self._profile.cgm.prediction_horizon_minutes)
                )
        elif meter is None:
            # No current glucose data: a stale low reading or an active low suspend needs a fresh meter check.
            if a.is_low(low):
                return "Last CGM reading was low and is no longer current - check with a meter first"
            if self._low_suspend_since is not None:
                return "Insulin is suspended for low glucose - enter a meter reading to bolus"
        return None

    def _bolus_refused(self, now, reason):
        self._audit(now, "bolus_refused", reason)
        return BolusResult(False, 0, reason)

    # ---- Hardware access -----------------------------------------------------------

    def _deliver(self, mu, kind, now):
        """Send a pre-authorized amount to the pump and verify the result."""
        error = None
        result = None
        try:
            result = self.pump.give_insulin(mu)
        except Exception as exc:  # noqa: BLE001 - any driver failure must fail safe
            error = "pump driver error: %r" % (exc,)
        if error is None and not (
            isinstance(result, DeliveryResult)
            and (result.success is True or result.success is False)
            and is_int(result.delivered_mu)
            and result.delivered_mu >= 0
        ):
            error = "pump driver returned an invalid response"
        if error is not None:
            # Outcome unknown: assume the full amount went in so IOB is never underestimated.
            self.ledger.record(Dose(now, mu, kind))
            self._latch_fault(
                now,
                "Delivery outcome unknown (%s); assuming %s U delivered"
                % (error, format_units(mu)),
            )
            return mu

        delivered = result.delivered_mu
        self.ledger.record(Dose(now, delivered, kind))
        if delivered > mu:
            self._latch_fault(
                now,
                "OVER-DELIVERY: requested %s U, pump reports %s U"
                % (format_units(mu, 3), format_units(delivered, 3)),
            )
        elif not result.success:
            self._latch_fault(
                now, "Delivery failed: %s" % (result.message or "unknown error")
            )
        elif delivered < mu:
            self._latch_fault(
                now,
                "Partial delivery: requested %s U, delivered %s U"
                % (format_units(mu, 3), format_units(delivered, 3)),
            )
        return delivered

    def _sensor_info(self, now):
        if self._sensor is None:
            try:
                self._sensor = SensorInfo(
                    self.cgm.get_sensor_info(),
                    self.cgm.get_trend_arrows(),
                    self._profile.cgm.max_plausible_rate,
                )
                self._audit(
                    now,
                    "cgm_info",
                    "range %d-%d mg/dL, arrows: %s"
                    % (
                        self._sensor.min_mg_dl,
                        self._sensor.max_mg_dl,
                        ", ".join(
                            "%s=%s" % (k, format_decimal(self._sensor.arrow_rates[k]))
                            for k in sorted(self._sensor.arrow_rates)
                        ),
                    ),
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 - unusable device info means unreliable CGM data
                self._audit(now, "cgm_info_error", repr(exc))
        return self._sensor

    def _assess(self, now):
        cgm = self._profile.cgm
        sensor = self._sensor_info(now)
        reading = self._call_cgm(now, self.cgm.get_current_glucose, None)
        history = self._call_cgm(
            now, lambda: self.cgm.get_glucose_history(cgm.history_window_minutes), []
        )
        trend = self._call_cgm(now, self.cgm.get_glucose_trend, None)
        return assess_glucose(
            reading,
            history if isinstance(history, list) else [],
            trend,
            now,
            cgm,
            sensor,
        )

    def _call_cgm(self, now, fn, default):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - CGM errors degrade to "no data"
            self._audit(now, "cgm_error", repr(exc))
            return default

    def _now(self):
        now = self.clock.now()
        if not is_int(now):
            raise TypeError("clock.now() must return integer seconds")
        if self._last_seen_time is not None and now < self._last_seen_time:
            self._latch_fault(
                now,
                "System clock moved backwards (%d s)" % (self._last_seen_time - now),
            )
        else:
            self._last_seen_time = now
        return now

    def _latch_fault(self, now, message):
        if self._fault is None:
            self._fault = message
            self._pending_bolus = None
            self._audit(now, "fault", message)

    def _audit(self, now, event, details):
        _bounded_append(
            self.audit_log,
            AuditEvent(now, event, details),
            self._profile.logging.max_audit_events,
        )


def _validated_int(value, name, low, high):
    if not is_int(value):
        raise ValueError("%s must be a whole number" % name)
    if not low <= value <= high:
        raise ValueError("%s must be between %d and %d" % (name, low, high))
    return value
