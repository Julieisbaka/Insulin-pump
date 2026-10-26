# Insulin-on-board (IOB) curve: linear vs exponential

Research notes behind the `insulin.curve` setting in the [profile schema](../insulin_pump/schemas/profile.schema.json).
**Simulation only - not medical advice.**

## Question

Is a linear or an exponential IOB curve safer for this pump? The two look similar around the middle of the
curve, but the question here is how they behave at *every* point after a dose.

## Why the direction of the error matters

IOB is subtracted from corrections and caps automatic dosing:

- **Under-estimating IOB** makes the pump think less insulin is active. It then gives more, which can stack
  doses and cause hypoglycaemia. This is the dangerous direction.
- **Over-estimating IOB** makes the pump give less. Glucose runs higher, which is the less immediately
  dangerous direction.

## The models

**Exponential.** This is the model used by Loop and OpenAPS. Dragan Maksimovic's formulation is implemented
identically in [LoopKit `ExponentialInsulinModel`](https://github.com/LoopKit/LoopKit/blob/dev/LoopKit/InsulinKit/ExponentialInsulinModel.swift)
and [oref0 `iobCalcExponential`](https://github.com/openaps/oref0/blob/master/lib/iob/calculate.js):

```
tau = tp * (1 - tp/td) / (1 - 2*tp/td)      td = action duration, tp = peak time
a   = 2 * tau / td
S   = 1 / (1 - a + (1 + a) * exp(-td/tau))
IOB(t) = 1 - S*(1-a)*((t^2/(tau*td*(1-a)) - t/tau - 1)*exp(-t/tau) + 1)
```

Typical presets:
- Loop's adult rapid-acting preset (Humalog/Novolog): 360 min duration, 75 min peak, 10 min delay.
- Fiasp/ultra-rapid: about 55 min peak.
- oref0 limits the peak to 50–120 min for rapid-acting insulin and 35–100 min for ultra-rapid.

**Linear.** IOB falls in a straight line to zero over the duration. Many pump bolus calculators use it. It has
no onset delay and no peak.

## Results across the whole curve

These values come from this repository's integer implementation: delay 10 min, duration 360 min, peak 75 min.
The "old linear 4 h" column is the previous version of this project.

| Minutes after dose | Old linear 4 h | Linear 6 h 10 min | Exponential (Loop adult) | Envelope (used) |
|---|---|---|---|---|
| 0 | 100% | 100% | 100% | 100% |
| 15 | 94% | 96% | 100% | 100% |
| 30 | 88% | 92% | 97% | 97% |
| 45 | 81% | 88% | 91% | 91% |
| 60 | 75% | 84% | 83% | 84% |
| 90 | 62% | 76% | 67% | 76% |
| 120 | 50% | 68% | 50% | 68% |
| 180 | 25% | 51% | 24% | 51% |
| 240 | 0% | 35% | 9% | 35% |
| 300 | 0% | 19% | 2% | 19% |
| 360 | 0% | 3% | 0% | 3% |

Findings:

1. **In the first hour, linear is the dangerous one.** With the same duration, linear is below the
   exponential curve for the first ~59 minutes, by up to ~5% of the dose. Insulin barely acts before its onset
   delay, but linear decay starts immediately. This is exactly the window in which people re-bolus or correct
   after a meal, so it is when stacking risk is highest.
2. **After about an hour, linear over-estimates heavily**, by up to ~28% of the dose. This direction is safe,
   but corrections come out smaller.
3. **A duration that is too short is the bigger hazard.** The old 4 h linear setting is below the realistic
   curve both in the first hour and after 4 h, so it under-counts at both ends. Walsh, Roberts & Heinemann
   (2014), [*Confusion Regarding Duration of Insulin Action*](https://doi.org/10.1177/1932296813514319), J
   Diabetes Sci Technol 8(1):170-178, found that pump users often set the duration too short (3–4 h). This
   under-counts IOB and leads to stacking and hypoglycaemia. The authors recommend at least about 5 h for
   rapid-acting analogues.
4. The exponential curve is the more physiologically accurate model, and Loop, OpenAPS, AndroidAPS and iAPS
   all use it. Accuracy alone, though, does not decide which model is safer.

## Decision

- **Default curve: `envelope`.** At every minute the pump uses the larger of the exponential and linear
  values. It can never be below either model, so it takes the safe side wherever the two disagree. This
  matches the project's rule that the default action is no insulin. The cost is somewhat smaller automatic
  corrections in the middle of the curve, so glucose runs higher.
- `exponential` and `linear` remain selectable in the profile.
- The schema enforces a minimum `duration_minutes` of 300 (5 h). Peak time is limited to 35–120 min, which
  covers oref0's ultra-rapid and rapid-acting limits. `delay_minutes` defaults to 10, matching Loop.
- Rounding is also conservative:
  - each table entry is rounded **up** and given 1 ppb of extra margin;
  - look-ups use elapsed minutes rounded **down** (the curve never increases, so this means more IOB);
  - per-dose IOB in milliunits is rounded **up**;
  - doses timestamped in the future count in full.
- The table is computed once per profile using only integer arithmetic: a fixed-point series for `exp` at
  10^-36 precision. Tests check it against the floating-point reference formula, to within 3 ppb and never
  below it.
