# Insulin Pump Controller (simulation)

> **⚠️ SIMULATION ONLY - NOT FOR MEDICAL USE.**
> This code has not been clinically validated, certified (IEC 62304 / ISO 14971 / FDA / CE) or tested on real
> hardware. Never use it to dose a person.

This is a headless (no UI) closed-loop insulin pump controller in pure Python. The CGM (blood glucose: current
reading, history and trend direction) and the insulin motor are mock devices, so the whole system can run as a
24-hour simulation.

The same code runs unchanged on **CPython** and on **MicroPython** microcontrollers such as the **Raspberry
Pi Pico**. It uses no floating point in any safety decision.

## Guiding principle: the default action is *no insulin*

Low glucose is more immediately dangerous than high glucose. Insulin is delivered only when every check
positively passes; any doubt, error, missing data or unexpected exception withholds it. Every unavoidable
rounding goes toward less insulin.

## Layout

| Path | Purpose |
| --- | --- |
| [insulin_pump/exact.py](insulin_pump/exact.py) | Exact arithmetic: rationals (`Q`), exact decimal parsing, rounding toward less insulin |
| [insulin_pump/insulin_curve.py](insulin_pump/insulin_curve.py) | IOB curves (envelope / exponential / linear), tabulated with integer-only maths |
| [insulin_pump/schemas/profile.schema.json](insulin_pump/schemas/profile.schema.json) | **Profile schema**: JSON Schema 2020-12 defining structure, hard limits and default values |
| [insulin_pump/schema.py](insulin_pump/schema.py) | Small schema validator that runs on MicroPython and refuses unsupported keywords |
| [insulin_pump/schedule.py](insulin_pump/schedule.py) | Time-of-day schedules: a default value plus partial-day segments, including segments that wrap past midnight |
| [insulin_pump/profile.py](insulin_pump/profile.py) | `TherapyProfile`: schema validation, defaults, exact unit conversion and cross-field checks |
| [insulin_pump/glucose.py](insulin_pump/glucose.py) | CGM data quality checks, exact least-squares trend and prediction |
| [insulin_pump/safety.py](insulin_pump/safety.py) | Independent dose gate, bounded delivery ledger and IOB |
| [insulin_pump/controller.py](insulin_pump/controller.py) | Pump state machine, bolus calculator and delivery verification |
| [insulin_pump/mocks.py](insulin_pump/mocks.py) | `MockCGM` (patient model, trend-arrow table, sensor range) and `MockInsulinDelivery` (motor, hardware limits, fault injection) |
| [insulin_pump/simulate.py](insulin_pump/simulate.py) | 24-hour console scenario with meals, a CGM dropout and an occlusion |
| [profiles/example_profile.json](profiles/example_profile.json) | Example therapy profile |
| [docs/iob-curve-research.md](docs/iob-curve-research.md) | Research comparing linear and exponential IOB curves, and the decision taken |
| [tools/](tools/) | MicroPython smoke test and a WebAssembly runner for checking without a Pico |

## Exact arithmetic

MicroPython on the Pico uses *single-precision* floats (about 7 significant digits), and even double precision
cannot represent 0.1 exactly. So:

- **Insulin** is integer **milliunits** (1 U = 1000 mU).
- **Glucose** is integer mg/dL.
- **Time** is integer seconds.
- **Ratios** (carb ratio, correction factor, rates of change, fractions) are exact rationals built on Python's
  arbitrary-precision integers.
- **Profile numbers** are parsed exactly from their decimal text. A value with more decimals than the schema
  allows is rejected, never silently rounded.

Rounding direction is always the safe one:

| Quantity | Direction |
| --- | --- |
| Insulin to deliver | Down, to the pump's increment |
| Insulin on board | Up |
| Predicted glucose | Down |

For example, the sub-increment basal remainder is carried forward as an exact fraction, so 0.85 U/hr
delivers exactly 0.85 U every hour.

The simulation's mock *patient* uses floats; it is the simulated body, not part of the safety logic. Its log
is identical on CPython and MicroPython, line for line.

## Profile and schema

The profile is JSON. The [schema](insulin_pump/schemas/profile.schema.json) is the single source of truth for
structure, hard limits and defaults. Editors such as VS Code read it through the profile's `$schema` key, and
the firmware enforces the same schema at load time.

### Schedules

Each schedule has an optional `default` and a list of `segments`, so a value can apply to only part of the day.

| Schedule | Unit | Meaning |
| --- | --- | --- |
| `basal_rate` | U/hr | Background insulin |
| `carb_ratio` | g/U | Grams of carbohydrate covered by 1 U |
| `correction_factor` | mg/dL per U | How far 1 U lowers glucose |
| `target` | mg/dL | Glucose that corrections aim for |
| `correct_above` | mg/dL | Corrections are only given above this value |
| `low_threshold` | mg/dL | At or below this value, now or predicted, insulin is suspended and boluses are blocked |

```json
"carb_ratio": { "default": 12, "segments": [ { "start": "06:00", "end": "11:00", "value": 9 } ] },
"target":     { "default": 110, "segments": [ { "start": "22:00", "end": "06:00", "value": 120 } ] }
```

- Times are `"HH:MM"` local time. A segment covers `start` up to, but not including, `end`.
- If `end` is earlier than `start`, the segment wraps past midnight. As an end time, `"24:00"` or `"00:00"` means midnight.
- `default` applies outside all segments. Without a default, the segments must cover the whole day.
- Overlapping segments are rejected.

### Settings sections

Every section is optional; omitted settings take the schema's `default`.

| Section | Contents |
| --- | --- |
| `insulin` | IOB `curve` (`envelope`, the default, / `exponential` / `linear`), `duration_minutes` (≥ 300), `peak_minutes`, `delay_minutes` |
| `limits` | Max bolus, max basal rate, max IOB, max hourly and daily insulin, max carbs |
| `alarms` | Urgent low, high alert, low and critical reservoir, how long a low suspend lasts before a critical alarm, critical alarm after insulin has been withheld for missing CGM data |
| `automation` | `enabled`, `correction_fraction`, `max_correction_per_cycle_units` |
| `cgm` | Maximum reading age, future-timestamp tolerance, implausible rate, history/rate windows, prediction horizon, `rate_combination` (`lower` / `average`), `fallback_basal_minutes` (default **0**) |
| `low_suspend` | `resume_margin_mg_dl` and `resume_min_rate` |
| `bolus` | Recommendation lifetime, `require_glucose`, meter range |
| `loop` | `cycle_minutes`, minimum cycle spacing, missed-cycle alert, longest timed suspend |
| `logging` | Bounded log sizes (fixed memory use on a microcontroller) |

Device facts are **fetched from the devices** rather than hard-coded. From the CGM:

- `get_sensor_info()`: measurable range;
- `get_trend_arrows()`: the rate each trend arrow stands for.

From the pump:

- `get_pump_info()`: dose increment, hardware single, hourly and daily limits, reservoir capacity.

### Validation

A profile is rejected as a whole, with every problem listed, if:

- it breaks the schema: wrong type, out-of-range value, too many decimals, unknown or missing key, NaN or Infinity;
- it fails a cross-field rule, checked at every schedule change point:
  - `urgent_low < low_threshold < target ≤ correct_above < high_alert`;
  - basal ≤ max basal rate;
  - the daily limit allows the scheduled basal plus one max bolus;
  - the automatic correction cap ≤ max bolus;
  - the trend windows are consistent;
- it exceeds the connected pump's hardware limits.

## Safety systems

**Glucose data**

- Readings count as untrusted when they are any of the following:
  - missing or non-integer;
  - stale or future-dated;
  - outside the sensor's range (LO/HI);
  - an implausible jump;
  - without a usable trend (too little history and no recognised arrow).
- The trend is an exact least-squares slope combined with the CGM's arrow. By default the *lower* of the two is
  used.
- A low value counts as low even when the reading is stale, future-dated or implausible.

**Pump modes** (checked in priority order every cycle)

1. `fault`: delivery is stopped until the user clears the fault.
2. `user_suspend`: suspended by the user, optionally for a set time.
3. `low_suspend`: glucose is at or below the low threshold now, or predicted to be within the horizon. This is
   latched. It resumes only when reliable data shows glucose at least `resume_margin` above the low threshold
   and not falling fast. A critical alarm is raised if it lasts too long.
4. When CGM data is unreliable:
   - `no_cgm_suspend`: the **default**. No insulin, plus a critical alarm about ketone/DKA risk after
     `no_insulin_critical_minutes`.
   - `fallback_basal`: used only if `cgm.fallback_basal_minutes > 0`. Scheduled basal is given for that long
     after the last reliable reading, then the pump suspends.
5. `closed_loop`:
   - Basal tapers to zero as the prediction approaches the low threshold.
   - Small automatic corrections are given only when both current and predicted glucose are above
     `correct_above`. They are sized from the lower of the two, minus IOB.

**Independent dose gate** ([safety.py](insulin_pump/safety.py)): every milliunit is checked here immediately
before delivery.

- Caps per dose type: max bolus, max basal rate × cycle length, automatic correction per cycle.
- Rolling 1-hour and 24-hour totals.
- Max IOB for non-basal insulin.
- The pump's own hardware limits.
- Amounts are rounded down to the motor increment.

**Insulin on board**

- Uses the `envelope` curve by default: at every minute, the larger of the Loop/OpenAPS exponential model and
  linear decay. See [docs/iob-curve-research.md](docs/iob-curve-research.md).
- Duration must be at least 5 h.

**Verification and fail-safe**

- Every pump response is checked. A failure, partial delivery, over-delivery, driver exception or malformed
  response latches a `FAULT`.
- If the outcome is unknown, the full dose is assumed to have been delivered.
- An unexpected internal exception latches a fault and nothing is delivered.
- If the clock moves backwards, a fault is latched.
- Missed cycles are never made up, and an early or duplicate cycle delivers nothing.
- Basal is prorated exactly across schedule changes that fall inside a cycle.

**Logging**

- Audit and decision logs have fixed sizes; old ledger entries are pruned.
- Alerts include urgent low, high, low and critical reservoir, and reservoir level unknown.

## Bolus workflow (calculate → confirm)

```python
rec = controller.calculate_bolus(carbs_g=45, meter_glucose=None)   # nothing is delivered yet
print(rec.summary(), rec.warnings)
result = controller.confirm_bolus(rec.id)                          # or confirm_bolus(rec.id, override_mu=3500)
```

- The bolus is: carbs ÷ carb ratio, plus a correction. Above `correct_above`, the correction is
  (glucose − target) ÷ correction factor, minus IOB. Below target, the correction is negative and reduces the
  meal dose. IOB never reduces the carb portion. Every step is exact and rounded toward less insulin.
- The bolus is **blocked** when:
  - there is no reliable glucose value (CGM or meter), which is configurable with `bolus.require_glucose`;
  - glucose is at or below the low threshold, the CGM reads LO, or a low is predicted;
  - the last reading was low but is no longer current, unless a meter reading is entered;
  - insulin is low-suspended and there is no CGM data, unless a meter reading is entered;
  - the pump is in a fault or suspended.
- A recommendation can be used once and expires.
- It is refused at confirmation if any of these happened since it was calculated:
  - a bolus or automatic correction was delivered;
  - a scheduled value changed;
  - glucose became low or is predicted low.
- An override must be a whole number of pump increments. If it exceeds a limit, it is refused rather than
  silently reduced.

## Running on a computer (CPython 3.9+)

```powershell
python -m insulin_pump.simulate                 # full 24 h log
python -m insulin_pump.simulate --quiet         # summary only
python -m insulin_pump.simulate --profile profiles\example_profile.json --seed 7 --hours 12 --no-faults
python -m unittest                              # 129 tests
pip install -r requirements.txt                 # optional dev tools (jsonschema cross-check, Pico deployment)
```

## Running on a Raspberry Pi Pico (MicroPython)

```powershell
mpremote cp -r insulin_pump :                   # package, including schemas/profile.schema.json
mpremote cp -r profiles :
mpremote run tools\micropython_smoke.py         # exact-maths and safety smoke test plus a 24 h simulation
mpremote exec "from insulin_pump import simulate; simulate.run(hours=2)"
```

- On a Pico (RP2040), precompile the modules with `mpy-cross` to save RAM, but keep the `.json` files.
  For a real device, freeze them into the firmware.
- Measured with the MicroPython 1.29 heap (32-bit):
  - about 82 KB for loaded code;
  - +22 KB for the profile, schema and insulin-curve table;
  - +38 KB for a running 24 h simulation (bounded logs and mocks).
- That fits an RP2040 Pico (about 190 KB of heap). A Pico 2 (RP2350, 520 KB) has ample headroom.
- Real devices would use `SystemClock(utc_offset_minutes=...)`, with the RTC set.
- Without a board you can run the same smoke test in MicroPython's official WebAssembly build:
  `npm install --no-save @micropython/micropython-webassembly-pyscript` and then
  `node tools/run_micropython_wasm.mjs`.

To connect real devices, replace the mocks with objects that provide the same methods:

- **CGM:** `get_current_glucose()`, `get_glucose_history(minutes)`, `get_glucose_trend()`,
  `get_sensor_info()` and `get_trend_arrows()`.
- **Pump:** `get_pump_info()`, `get_reservoir_mu()` and `give_insulin(mu) -> DeliveryResult`.
