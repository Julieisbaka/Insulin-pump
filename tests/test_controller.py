import unittest

from insulin_pump.exact import Q
from insulin_pump.models import DeliveryResult, DeviceInfoError, Dose, DoseKind, Mode, SensorStatus, Severity
from insulin_pump.profile import ProfileValidationError
from insulin_pump.simulate import run

from .helpers import START, make_profile, make_pump, run_cycles, seg

FLAT = [110, 110, 110]


def codes(decision):
    return decision.alert_codes()


class ClosedLoopTests(unittest.TestCase):
    def test_in_range_delivers_exact_scheduled_basal(self):
        c, cgm, _, clock = make_pump()
        decisions = run_cycles(c, cgm, clock, FLAT, 12)
        self.assertTrue(all(d.mode == Mode.CLOSED_LOOP for d in decisions))
        self.assertEqual(sum(d.delivered_mu for d in decisions), 1200)
        self.assertTrue(all(d.correction_mu == 0 for d in decisions))

    def test_sub_increment_basal_is_carried_exactly(self):
        # 0.85 U/hr = 70.83... mU per 5 min; the pump moves in 50 mU steps.
        c, cgm, _, clock = make_pump(make_profile(basal_rate={"default": 0.85}))
        decisions = run_cycles(c, cgm, clock, FLAT, 12)
        self.assertEqual(sum(d.delivered_mu for d in decisions), 850)
        self.assertEqual(c._basal_carry, 0)

    def test_basal_follows_time_of_day_schedule(self):
        profile = make_profile(basal_rate={"default": 0.6, "segments": [seg("12:00", "13:00", 2.4)]})
        c, cgm, _, clock = make_pump(profile)
        first_hour = run_cycles(c, cgm, clock, FLAT, 12)
        second_hour = run_cycles(c, cgm, clock, FLAT, 12)
        self.assertEqual(sum(d.delivered_mu for d in first_hour), 2400)
        self.assertEqual(sum(d.delivered_mu for d in second_hour), 600)

    def test_correction_only_above_correct_above(self):
        c, _, _, _ = make_pump(values=[140, 140, 140])  # above target 110, below correct-above 150
        self.assertEqual(c.run_cycle().correction_mu, 0)
        c, _, _, _ = make_pump(values=[200, 200, 200])
        d = c.run_cycle()
        self.assertEqual(d.correction_mu, 500)  # 0.3 * (200-110)/50 U = 0.54 U, capped at 0.5 U per cycle

    def test_correction_uses_lower_of_current_and_predicted(self):
        c, _, _, _ = make_pump(values=[180, 190, 200], trend="SINGLE_UP")  # predicted 260, current 200
        d = c.run_cycle()
        self.assertEqual(d.predicted_glucose, 260)
        self.assertEqual(d.correction_mu, 500)
        c, _, _, _ = make_pump(make_profile(automation={"correction_fraction": 0.1}), values=[180, 190, 200], trend="SINGLE_UP")
        self.assertEqual(c.run_cycle().correction_mu, 180)  # 0.1 * (200-110)/50 U, not based on 260

    def test_correct_above_follows_schedule(self):
        profile = make_profile(correct_above={"default": 150, "segments": [seg("12:00", "13:00", 220)]})
        c, _, _, _ = make_pump(profile, values=[200, 200, 200])
        self.assertEqual(c.run_cycle().correction_mu, 0)

    def test_automation_disabled_means_no_corrections(self):
        c, _, _, _ = make_pump(make_profile(automation={"enabled": False}), values=[300, 300, 300])
        d = c.run_cycle()
        self.assertEqual(d.correction_mu, 0)
        self.assertEqual(d.delivered_mu, 100)

    def test_iob_reduces_automatic_correction(self):
        c, cgm, _, clock = make_pump(values=[250, 250, 250])
        first = c.run_cycle()
        c.ledger.record(Dose(clock.now(), 3000, DoseKind.BOLUS))
        clock.advance(5)
        cgm.set([250, 250, 250])
        self.assertLess(c.run_cycle().correction_mu, first.correction_mu)

    def test_basal_tapers_when_heading_below_target(self):
        c, _, _, _ = make_pump(values=[115, 110, 105], trend="FORTY_FIVE_DOWN")  # predicted 75
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.CLOSED_LOOP)
        self.assertEqual(d.basal_request, d.scheduled_basal * Q(5, 40))

    def test_lower_of_slope_and_arrow_is_used(self):
        c, _, _, _ = make_pump(values=FLAT, trend="SINGLE_DOWN")  # slope 0, arrow -2 -> predicted 50
        self.assertEqual(c.run_cycle().mode, Mode.LOW_SUSPEND)
        c, _, _, _ = make_pump(make_profile(cgm={"rate_combination": "average"}), values=FLAT, trend="SINGLE_DOWN")
        self.assertEqual(c.run_cycle().mode, Mode.CLOSED_LOOP)  # average -1 -> predicted 80


class LowProtectionTests(unittest.TestCase):
    def test_low_suspends_with_critical_alert(self):
        c, _, pump, _ = make_pump(values=[80, 75, 68], trend="FORTY_FIVE_DOWN")
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.LOW_SUSPEND)
        self.assertEqual(d.delivered_mu, 0)
        self.assertEqual(pump.delivery_log, [])
        self.assertIn("low", codes(d))

    def test_personal_low_threshold_is_used(self):
        c, _, _, _ = make_pump(make_profile(low_threshold={"default": 90}), values=[88, 88, 88])
        self.assertEqual(c.run_cycle().mode, Mode.LOW_SUSPEND)

    def test_low_threshold_follows_schedule(self):
        profile = make_profile(low_threshold={"default": 70, "segments": [seg("12:00", "13:00", 90)]})
        c, cgm, _, clock = make_pump(profile, values=[85, 85, 85])
        self.assertEqual(c.run_cycle().mode, Mode.LOW_SUSPEND)
        c.resume()
        clock.advance(60)
        cgm.set([85, 85, 85])
        self.assertEqual(c.run_cycle().mode, Mode.CLOSED_LOOP)

    def test_predicted_low_suspends(self):
        c, _, _, _ = make_pump(values=[130, 120, 110, 100], trend="SINGLE_DOWN")
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.LOW_SUSPEND)
        self.assertIn("predicted_low", codes(d))

    def test_sensor_lo_suspends(self):
        c, _, _, _ = make_pump(values=[39])
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.LOW_SUSPEND)
        self.assertIn("urgent_low", codes(d))

    def test_urgent_low_alarm(self):
        c, _, _, _ = make_pump(values=[54, 54, 54])
        self.assertIn("urgent_low", codes(c.run_cycle()))

    def test_resume_needs_margin_and_reliable_data(self):
        c, cgm, _, clock = make_pump(values=[68, 68, 68])
        self.assertEqual(c.run_cycle().mode, Mode.LOW_SUSPEND)
        clock.advance(5)
        self.assertEqual(run_cycles(c, cgm, clock, [80, 80, 80], 1)[0].mode, Mode.LOW_SUSPEND)  # needs low + 20
        cgm.history = []
        self.assertEqual(c.run_cycle().mode, Mode.LOW_SUSPEND)  # CGM lost: stays latched
        clock.advance(5)
        d = run_cycles(c, cgm, clock, [95, 95, 95], 1)[0]
        self.assertEqual(d.mode, Mode.CLOSED_LOOP)
        self.assertIn("low_resume", codes(d))

    def test_no_resume_while_falling_fast(self):
        c, cgm, _, clock = make_pump(values=[68, 68, 68])
        c.run_cycle()
        clock.advance(5)
        profile_rate_floor = make_profile().low_suspend.resume_min_rate
        self.assertEqual(profile_rate_floor, -1)
        d = run_cycles(c, cgm, clock, [180, 175, 170], 1, trend="FORTY_FIVE_DOWN")[0]  # -1 mg/dL/min: not > -1
        self.assertEqual(d.mode, Mode.LOW_SUSPEND)

    def test_long_suspend_escalates(self):
        c, cgm, _, clock = make_pump(values=[68, 68, 68])
        decisions = run_cycles(c, cgm, clock, [68, 68, 68], 26)
        self.assertNotIn("long_suspend", codes(decisions[0]))
        self.assertIn("long_suspend", codes(decisions[-1]))

    def test_stale_low_reading_suspends(self):
        c, cgm, _, _ = make_pump()
        cgm.set([75, 68, 60], trend="SINGLE_DOWN", age_min=30)
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.LOW_SUSPEND)
        self.assertEqual(d.delivered_mu, 0)


class CgmLossTests(unittest.TestCase):
    """Default action is no insulin: unreliable CGM data withholds insulin unless a fallback window is configured."""

    def test_no_cgm_withholds_insulin_by_default(self):
        c, cgm, pump, _ = make_pump(values=[300, 300, 300])
        cgm.history = []
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.NO_CGM_SUSPEND)
        self.assertEqual(d.delivered_mu, 0)
        self.assertEqual(pump.delivery_log, [])

    def test_stale_erroring_high_and_no_trend_all_withhold(self):
        c, cgm, _, clock = make_pump()
        clock.advance(20)
        d = c.run_cycle()
        self.assertEqual((d.mode, d.sensor_status), (Mode.NO_CGM_SUSPEND, SensorStatus.STALE))
        cgm.raise_errors = True
        clock.advance(5)
        self.assertEqual(c.run_cycle().mode, Mode.NO_CGM_SUSPEND)
        self.assertTrue(any(e.event == "cgm_error" for e in c.audit_log))
        for values, trend, status in (([401], "FLAT", SensorStatus.SENSOR_HIGH), ([150], "UNKNOWN_ARROW", SensorStatus.NO_TREND)):
            c, _, _, _ = make_pump(values=values, trend=trend)
            d = c.run_cycle()
            self.assertEqual((d.mode, d.sensor_status, d.delivered_mu), (Mode.NO_CGM_SUSPEND, status, 0))

    def test_cgm_device_info_unavailable_withholds(self):
        c, cgm, _, _ = make_pump()
        cgm.arrows = {}
        d = c.run_cycle()
        self.assertEqual((d.mode, d.delivered_mu), (Mode.NO_CGM_SUSPEND, 0))
        self.assertTrue(any(e.event == "cgm_info_error" for e in c.audit_log))

    def test_configured_fallback_window_then_suspend(self):
        c, cgm, _, clock = make_pump(make_profile(cgm={"fallback_basal_minutes": 30}))
        self.assertEqual(c.run_cycle().mode, Mode.CLOSED_LOOP)
        cgm.history = []
        modes = []
        for _ in range(8):
            clock.advance(5)
            modes.append(c.run_cycle().mode)
        self.assertEqual(modes[:5], [Mode.FALLBACK_BASAL] * 5)
        self.assertEqual(modes[5:], [Mode.NO_CGM_SUSPEND] * 3)

    def test_fallback_needs_a_previous_reliable_reading(self):
        c, cgm, _, _ = make_pump(make_profile(cgm={"fallback_basal_minutes": 30}))
        cgm.history = []
        self.assertEqual(c.run_cycle().mode, Mode.NO_CGM_SUSPEND)

    def test_withholding_escalates_to_critical(self):
        c, cgm, _, clock = make_pump()
        cgm.history = []
        decisions = []
        for _ in range(8):
            decisions.append(c.run_cycle())
            clock.advance(5)
        self.assertNotIn("no_insulin", codes(decisions[0]))
        self.assertIn("no_insulin", codes(decisions[6]))  # 30 min (profile default)
        self.assertTrue(any(a.severity == Severity.CRITICAL for a in decisions[6].alerts))


class FaultTests(unittest.TestCase):
    def test_occlusion_latches_fault_until_cleared(self):
        c, cgm, pump, clock = make_pump()
        pump.occluded = True
        d = c.run_cycle()
        self.assertEqual(d.mode, Mode.FAULT)
        self.assertTrue(any(a.severity == Severity.CRITICAL for a in d.alerts))
        pump.occluded = False
        clock.advance(5)
        later = run_cycles(c, cgm, clock, FLAT, 3)
        self.assertTrue(all(x.mode == Mode.FAULT and x.delivered_mu == 0 for x in later))
        self.assertTrue(c.clear_fault())
        self.assertEqual(run_cycles(c, cgm, clock, FLAT, 1)[0].mode, Mode.CLOSED_LOOP)

    def test_over_delivery_detected_and_recorded(self):
        c, _, pump, _ = make_pump()
        pump.over_deliver_mu = 200
        self.assertEqual(c.run_cycle().mode, Mode.FAULT)
        self.assertIn("OVER-DELIVERY", c.fault)
        self.assertEqual(sum(x.mu for x in c.ledger.doses), 300)

    def test_partial_delivery_faults(self):
        c, _, pump, _ = make_pump()
        pump.partial_deliver_mu = 50
        c.run_cycle()
        self.assertIn("Partial", c.fault)

    def test_driver_exception_assumes_full_delivery(self):
        c, _, pump, _ = make_pump(values=[250, 250, 250])
        pump.raise_next = True
        c.run_cycle()
        self.assertIn("unknown", c.fault)
        self.assertEqual(sum(x.mu for x in c.ledger.doses), 100)

    def test_invalid_driver_responses_fault(self):
        c, _, pump, _ = make_pump()
        pump.invalid_next = True
        c.run_cycle()
        self.assertIsNotNone(c.fault)
        c, _, pump, _ = make_pump()
        pump.give_insulin = lambda mu: DeliveryResult(True, float(mu))
        c.run_cycle()
        self.assertIn("invalid", c.fault)

    def test_internal_error_stops_insulin(self):
        c, _, pump, _ = make_pump(values=[250, 250, 250])

        def broken(*_args):
            raise ZeroDivisionError("bug")

        c._closed_loop_request = broken
        d = c.run_cycle()
        self.assertEqual((d.mode, d.delivered_mu), (Mode.FAULT, 0))
        self.assertEqual(pump.delivery_log, [])
        self.assertIn("internal", c.fault)

    def test_clock_moving_backwards_faults(self):
        c, cgm, _, clock = make_pump()
        c.run_cycle()
        clock.advance(-30)
        cgm.set(FLAT)
        self.assertEqual(c.run_cycle().mode, Mode.FAULT)
        self.assertIn("clock", c.fault)

    def test_missed_cycles_are_not_caught_up(self):
        c, cgm, _, clock = make_pump()
        c.run_cycle()
        clock.advance(60)
        cgm.set(FLAT)
        d = c.run_cycle()
        self.assertIn("missed_cycles", codes(d))
        self.assertLessEqual(d.delivered_mu, 100)

    def test_low_reservoir_alert(self):
        c, _, _, _ = make_pump(reservoir_mu=10000)
        self.assertIn("reservoir", codes(c.run_cycle()))

    def test_invalid_device_info_refused(self):
        with self.assertRaises(DeviceInfoError):
            make_pump(increment_mu=0)

    def test_profile_beyond_pump_limits_refused(self):
        with self.assertRaisesRegex(ProfileValidationError, "max single delivery"):
            make_pump(max_single_delivery_mu=5000)


class UserControlTests(unittest.TestCase):
    def test_suspend_and_resume(self):
        c, cgm, pump, clock = make_pump()
        c.suspend()
        self.assertEqual(c.run_cycle().mode, Mode.USER_SUSPEND)
        self.assertEqual(pump.delivery_log, [])
        c.resume()
        clock.advance(5)
        self.assertEqual(run_cycles(c, cgm, clock, FLAT, 1)[0].mode, Mode.CLOSED_LOOP)

    def test_timed_suspend_ends(self):
        c, cgm, _, clock = make_pump()
        c.suspend(minutes=30)
        decisions = run_cycles(c, cgm, clock, FLAT, 8)
        self.assertEqual(decisions[0].mode, Mode.USER_SUSPEND)
        self.assertEqual(decisions[-1].mode, Mode.CLOSED_LOOP)
        with self.assertRaises(ValueError):
            c.suspend(minutes=0.5)

    def test_profile_update_validated_and_audited(self):
        c, _, _, _ = make_pump()
        with self.assertRaises(TypeError):
            c.update_profile({"basal_rate": 5})
        c.update_profile(make_profile(name="Weekend"))
        self.assertEqual(c.profile.name, "Weekend")
        self.assertEqual(c.audit_log[-1].event, "profile_changed")


class BolusTests(unittest.TestCase):
    def test_carbs_use_time_of_day_carb_ratio(self):
        c, _, _, _ = make_pump(make_profile(carb_ratio={"default": 12, "segments": [seg("12:00", "14:00", 8)]}))
        rec = c.calculate_bolus(48)
        self.assertEqual(rec.carb_ratio, 8)
        self.assertEqual(rec.recommended_mu, 6000)

    def test_fractional_carb_ratio_is_exact_and_rounds_down(self):
        c, _, _, _ = make_pump(make_profile(carb_ratio={"default": 7.5}))
        rec = c.calculate_bolus(50)  # 6.666... U
        self.assertEqual(rec.carb_mu, 6666)
        self.assertEqual(rec.recommended_mu, 6650)

    def test_correction_above_correct_above_minus_iob(self):
        c, _, _, _ = make_pump(values=[210, 210, 210])
        rec = c.calculate_bolus(0)
        self.assertEqual(rec.correction_mu, 2000)
        self.assertEqual(c.confirm_bolus(rec.id).delivered_mu, 2000)
        self.assertEqual(c.calculate_bolus(0).recommended_mu, 0)  # the 2 U on board already covers it

    def test_no_correction_between_target_and_correct_above(self):
        c, _, _, _ = make_pump(values=[140, 140, 140])
        self.assertEqual(c.calculate_bolus(30).recommended_mu, 3000)

    def test_below_target_reduces_meal_bolus(self):
        c, _, _, _ = make_pump(values=[85, 85, 85])
        rec = c.calculate_bolus(30)
        self.assertEqual(rec.correction_mu, -500)
        self.assertEqual(rec.recommended_mu, 2500)

    def test_bolus_blocked_when_low(self):
        c, _, _, _ = make_pump(values=[65, 65, 65])
        rec = c.calculate_bolus(30)
        self.assertTrue(rec.blocked)
        self.assertEqual(rec.recommended_mu, 0)
        self.assertFalse(c.confirm_bolus(rec.id).success)
        self.assertTrue(make_pump(values=[39])[0].calculate_bolus(30).blocked)
        self.assertTrue(make_pump()[0].calculate_bolus(30, meter_glucose=60).blocked)

    def test_bolus_needs_glucose_by_default(self):
        c, cgm, _, _ = make_pump()
        cgm.history = []
        rec = c.calculate_bolus(30)
        self.assertTrue(rec.blocked)
        self.assertIn("meter", rec.blocked_reason)
        rec = c.calculate_bolus(30, meter_glucose=210)
        self.assertEqual((rec.glucose_source, rec.recommended_mu), ("meter", 5000))

    def test_carbs_only_bolus_when_glucose_not_required(self):
        c, cgm, _, _ = make_pump(make_profile(bolus={"require_glucose": False}))
        cgm.history = []
        rec = c.calculate_bolus(30)
        self.assertEqual((rec.glucose_source, rec.correction_mu, rec.recommended_mu), ("none", 0, 3000))
        self.assertTrue(rec.warnings)

    def test_recommendation_capped_at_max_bolus(self):
        c, _, _, _ = make_pump()
        rec = c.calculate_bolus(200)
        self.assertEqual(rec.recommended_mu, 10000)
        self.assertTrue(any("max bolus" in w for w in rec.warnings))

    def test_invalid_inputs_rejected(self):
        c, _, _, _ = make_pump()
        for bad in (-5, 251, 30.0, float("nan"), "50", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                c.calculate_bolus(bad)
        with self.assertRaises(ValueError):
            c.calculate_bolus(30, meter_glucose=900)

    def test_recommendation_single_use_and_latest_only(self):
        c, _, _, _ = make_pump()
        rec = c.calculate_bolus(30)
        self.assertTrue(c.confirm_bolus(rec.id).success)
        self.assertFalse(c.confirm_bolus(rec.id).success)
        self.assertFalse(c.confirm_bolus(12345).success)
        old = c.calculate_bolus(30)
        new = c.calculate_bolus(40)
        self.assertFalse(c.confirm_bolus(old.id).success)

    def test_recommendation_expires(self):
        c, _, _, clock = make_pump()
        rec = c.calculate_bolus(30)
        clock.advance(6)
        result = c.confirm_bolus(rec.id)
        self.assertFalse(result.success)
        self.assertIn("expired", result.message)

    def test_refused_while_suspended_or_faulted(self):
        c, _, pump, _ = make_pump()
        rec = c.calculate_bolus(30)
        c.suspend()
        self.assertFalse(c.confirm_bolus(rec.id).success)
        c.resume()
        pump.occluded = True
        c.run_cycle()
        self.assertTrue(c.calculate_bolus(30).blocked)

    def test_override_above_limit_refused_not_reduced(self):
        c, _, pump, _ = make_pump()
        rec = c.calculate_bolus(30)
        result = c.confirm_bolus(rec.id, override_mu=15000)
        self.assertFalse(result.success)
        self.assertIn("max bolus", result.message)
        self.assertEqual(pump.delivery_log, [])

    def test_override_must_be_whole_increments(self):
        c, _, _, _ = make_pump()
        rec = c.calculate_bolus(30)
        self.assertFalse(c.confirm_bolus(rec.id, override_mu=2025).success)
        rec = c.calculate_bolus(30)
        self.assertFalse(c.confirm_bolus(rec.id, override_mu=2.0).success)

    def test_override_within_limits_audited(self):
        c, _, _, _ = make_pump()
        rec = c.calculate_bolus(30)
        self.assertEqual(c.confirm_bolus(rec.id, override_mu=2000).delivered_mu, 2000)
        self.assertTrue(any(e.event == "bolus_override" for e in c.audit_log))

    def test_reservoir_checked(self):
        c, _, _, _ = make_pump(reservoir_mu=2000)
        result = c.confirm_bolus(c.calculate_bolus(30).id)
        self.assertFalse(result.success)
        self.assertIn("reservoir", result.message)

    def test_profile_change_invalidates_recommendation(self):
        c, _, _, _ = make_pump()
        rec = c.calculate_bolus(30)
        c.update_profile(make_profile(carb_ratio={"default": 5}))
        self.assertFalse(c.confirm_bolus(rec.id).success)


class HardeningTests(unittest.TestCase):
    def test_repeated_cycle_at_same_time_delivers_nothing(self):
        c, _, _, _ = make_pump()
        first = c.run_cycle()
        second = c.run_cycle()
        self.assertGreater(first.delivered_mu, 0)
        self.assertEqual(second.delivered_mu, 0)
        self.assertIn("cycle_too_soon", codes(second))
        self.assertEqual(len(c.ledger.doses), 1)

    def test_early_retry_does_not_shift_schedule(self):
        c, cgm, _, clock = make_pump()
        c.run_cycle()
        clock.advance(2)
        cgm.set(FLAT)
        self.assertEqual(c.run_cycle().delivered_mu, 0)
        clock.advance(3)
        cgm.set(FLAT)
        self.assertGreater(c.run_cycle().delivered_mu, 0)

    def test_bolus_blocked_during_low_suspend_without_data(self):
        c, cgm, _, clock = make_pump(values=[80, 72, 65], trend="SINGLE_DOWN")
        self.assertEqual(c.run_cycle().mode, Mode.LOW_SUSPEND)
        clock.advance(5)
        cgm.history = []
        rec = c.calculate_bolus(30)
        self.assertTrue(rec.blocked)
        self.assertIn("meter", rec.blocked_reason)
        rec = c.calculate_bolus(30, meter_glucose=130)
        self.assertFalse(rec.blocked)
        self.assertTrue(c.confirm_bolus(rec.id).success)

    def test_bolus_blocked_on_predicted_low(self):
        c, _, _, _ = make_pump(values=[110, 95, 80], trend="SINGLE_DOWN")
        rec = c.calculate_bolus(30)
        self.assertTrue(rec.blocked)
        self.assertIn("predicts", rec.blocked_reason)

    def test_confirm_refused_when_prediction_turns_low(self):
        c, cgm, _, clock = make_pump()
        rec = c.calculate_bolus(30)
        clock.advance(2)
        cgm.set([110, 95, 80], trend="SINGLE_DOWN")
        result = c.confirm_bolus(rec.id)
        self.assertFalse(result.success)
        self.assertEqual(result.delivered_mu, 0)

    def test_auto_correction_invalidates_pending_bolus(self):
        c, cgm, _, clock = make_pump(values=[250, 250, 250])
        rec = c.calculate_bolus(30)
        clock.advance(1)
        cgm.set([250, 250, 250])
        self.assertGreater(c.run_cycle().correction_mu, 0)
        result = c.confirm_bolus(rec.id)
        self.assertFalse(result.success)
        self.assertIn("recalculate", result.message)

    def test_schedule_boundary_between_calculate_and_confirm(self):
        c, _, _, clock = make_pump(make_profile(carb_ratio={"default": 10, "segments": [seg("12:02", "18:00", 5)]}))
        rec = c.calculate_bolus(30)
        self.assertEqual(rec.carb_mu, 3000)
        clock.advance(3)
        result = c.confirm_bolus(rec.id)
        self.assertFalse(result.success)
        self.assertIn("changed", result.message)

    def test_basal_prorated_across_rate_change(self):
        # Cycle starts at 11:58; rate goes from 1.2 to 2.4 U/hr at 12:00.
        profile = make_profile(basal_rate={"default": 1.2, "segments": [seg("12:00", "13:00", 2.4)]})
        c, _, _, clock = make_pump(profile)
        clock.set(START - 120)
        d = c.run_cycle()
        self.assertEqual(d.scheduled_basal, Q(1200 * 120 + 2400 * 180, 3600))

    def test_logs_are_bounded(self):
        c, cgm, _, clock = make_pump(make_profile(logging={"max_decisions": 12, "max_audit_events": 50}))
        run_cycles(c, cgm, clock, FLAT, 30)
        for _ in range(60):
            c.calculate_bolus(10)
        self.assertEqual(len(c.log), 12)
        self.assertEqual(len(c.audit_log), 50)


class SimulationTests(unittest.TestCase):
    def test_full_day_simulation_with_faults_stays_safe(self):
        for seed in range(4):
            readings = []
            controller = run(hours=24, seed=seed, verbose=False, summary=True, out=readings.append)
            cgm_line = [l for l in readings if l.startswith("CGM min/max")][0]
            minimum = int(cgm_line.split(":")[1].split("/")[0])
            self.assertGreater(minimum, 70, "seed %d" % seed)
            events = {e.event for e in controller.audit_log}
            self.assertTrue({"fault", "fault_cleared", "bolus_delivered", "no_cgm_suspend"} <= events)
            self.assertLessEqual(sum(controller.ledger.totals_mu.values()), controller.profile.limits.max_daily_mu)

    def test_simulation_is_deterministic(self):
        first, second = [], []
        run(hours=6, seed=5, verbose=True, out=first.append)
        run(hours=6, seed=5, verbose=True, out=second.append)
        self.assertEqual(first, second)

    def test_simulation_without_faults(self):
        controller = run(hours=24, seed=1, faults=False, verbose=False)
        self.assertNotIn(Mode.FAULT, {d.mode for d in controller.log})


if __name__ == "__main__":
    unittest.main()
