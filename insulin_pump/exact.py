"""Exact arithmetic for dosing maths (runs on CPython and MicroPython).

Floating point is never used for safety decisions. MicroPython on the
Raspberry Pi Pico uses *single precision* floats (about 7 significant digits),
and even double precision cannot represent values such as 0.1 exactly.
Instead:

* insulin is counted in integer **milliunits** (1 U = 1000 mU),
* glucose is integer mg/dL,
* time is integer seconds,
* ratios (carb ratio, correction factor, rates of change, fractions) are exact
  rationals (:class:`Q`) built on Python's arbitrary-precision integers.

Every conversion that must lose precision states its rounding direction and is
chosen so that the error can only lead to *less* insulin being given:
insulin to deliver rounds down, insulin on board rounds up, glucose estimates
round down.
"""

MU_PER_UNIT = 1000


def is_int(value):
    """True for real integers only (bool is excluded on every Python implementation)."""
    return type(value) is int


def _gcd(a, b):
    a = -a if a < 0 else a
    b = -b if b < 0 else b
    while b:
        a, b = b, a % b
    return a


class Q:
    """Exact rational number n/d with d > 0, always in lowest terms."""

    __slots__ = ("n", "d")

    def __init__(self, n, d=1):
        if not is_int(n) or not is_int(d):
            raise TypeError("Q needs integer numerator and denominator")
        if d == 0:
            raise ZeroDivisionError("Q denominator is zero")
        if d < 0:
            n, d = -n, -d
        g = _gcd(n, d)
        if g > 1:
            n //= g
            d //= g
        self.n = n
        self.d = d

    # -- arithmetic (the right-hand operand may be a Q or an int) --
    def __add__(self, o):
        o = as_q(o)
        return Q(self.n * o.d + o.n * self.d, self.d * o.d)

    def __sub__(self, o):
        o = as_q(o)
        return Q(self.n * o.d - o.n * self.d, self.d * o.d)

    def __mul__(self, o):
        o = as_q(o)
        return Q(self.n * o.n, self.d * o.d)

    def __truediv__(self, o):
        o = as_q(o)
        if o.n == 0:
            raise ZeroDivisionError("division by zero")
        return Q(self.n * o.d, self.d * o.n)

    def __neg__(self):
        return Q(-self.n, self.d)

    def __abs__(self):
        return Q(-self.n, self.d) if self.n < 0 else self

    # -- comparisons (all defined explicitly: MicroPython does not derive reflected ones) --
    def _cmp(self, o):
        o = as_q(o)
        left, right = self.n * o.d, o.n * self.d
        return (left > right) - (left < right)

    def __lt__(self, o):
        return self._cmp(o) < 0

    def __le__(self, o):
        return self._cmp(o) <= 0

    def __gt__(self, o):
        return self._cmp(o) > 0

    def __ge__(self, o):
        return self._cmp(o) >= 0

    def __eq__(self, o):
        if not (isinstance(o, Q) or is_int(o)):
            return False
        return self._cmp(o) == 0

    def __ne__(self, o):
        return not self.__eq__(o)

    def __hash__(self):
        return hash((self.n, self.d))

    # -- conversions --
    def floor(self):
        return self.n // self.d

    def ceil(self):
        return -((-self.n) // self.d)

    def is_integer(self):
        return self.d == 1

    def __repr__(self):
        return "Q(%d, %d)" % (self.n, self.d)

    def __str__(self):
        return format_decimal(self)


def as_q(value):
    if isinstance(value, Q):
        return value
    if is_int(value):
        return Q(value)
    raise TypeError(
        "expected an exact number (int or Q), got %s" % type(value).__name__
    )


def q_min(a, b):
    return a if as_q(a) <= as_q(b) else b


def q_max(a, b):
    return a if as_q(a) >= as_q(b) else b


def clamp(value, low, high):
    return q_max(low, q_min(value, high))


class DecimalError(ValueError):
    pass


def parse_decimal(value, max_decimals=None):
    """Convert a JSON value to an exact Q.

    Accepts ints, decimal strings such as "0.85", and floats. A float is converted
    via its shortest repr, which reproduces the number as written in the file.
    If that representation has more decimals than allowed (for example because a
    single-precision float could not hold it), the value is rejected rather than
    silently changed.
    """
    if is_int(value):
        return Q(value)
    if isinstance(value, float):
        if value != value or value - value != 0:
            raise DecimalError("must be a finite number")
        text = repr(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise DecimalError("must be a number")
    result, decimals = _parse_decimal_text(text)
    if max_decimals is not None and decimals > max_decimals:
        raise DecimalError(
            "has more than %d decimal place%s"
            % (max_decimals, "" if max_decimals == 1 else "s")
        )
    return result


def _parse_decimal_text(text):
    if not text:
        raise DecimalError("is empty")
    mantissa, exponent = text, 0
    for marker in ("e", "E"):
        if marker in text:
            mantissa, exp_text = text.split(marker, 1)
            exponent = _parse_int_text(exp_text, allow_sign=True)
            break
    sign = 1
    if mantissa[:1] in ("+", "-"):
        sign = -1 if mantissa[0] == "-" else 1
        mantissa = mantissa[1:]
    whole, _, frac = mantissa.partition(".")
    if not whole and not frac:
        raise DecimalError("'%s' is not a decimal number" % text)
    for part in (whole, frac):
        if part and not part.isdigit():
            raise DecimalError("'%s' is not a decimal number" % text)
    frac = frac.rstrip("0")
    numerator = int((whole or "0") + frac) * sign
    scale = len(frac) - exponent
    if scale <= 0:
        return Q(numerator * 10 ** (-scale)), 0
    result = Q(numerator, 10**scale)
    # Count decimals of the reduced value (e.g. "1.50e1" -> 15, 0 decimals).
    decimals = 0
    probe = result
    while not probe.is_integer():
        probe = probe * 10
        decimals += 1
        if decimals > 30:
            break
    return result, decimals


def _parse_int_text(text, allow_sign=False):
    body = text
    if allow_sign and body[:1] in ("+", "-"):
        body = body[1:]
    if not body or not body.isdigit():
        raise DecimalError("'%s' is not a whole number" % text)
    return int(text)


def to_scaled_int(value, scale, max_decimals):
    """Exact decimal -> integer in 1/scale units (e.g. U -> mU). Rejects values that don't fit exactly."""
    q = parse_decimal(value, max_decimals) * scale
    if not q.is_integer():
        raise DecimalError("cannot be represented exactly")
    return q.n


def format_decimal(value, places=None):
    """Human-readable decimal string. Display only - never parse this back for dosing."""
    q = as_q(value)
    if places is None:
        # Shortest exact representation if the value terminates within 6 places.
        for p in range(0, 7):
            scaled = q * (10**p)
            if scaled.is_integer():
                return _format_scaled(scaled.n, p)
        places = 6
    # Round half away from zero for display.
    scaled = q * (10**places)
    n = (abs(scaled) + Q(1, 2)).floor()
    return _format_scaled(-n if scaled.n < 0 else n, places)


def _format_scaled(n, places):
    sign = "-" if n < 0 else ""
    n = -n if n < 0 else n
    if places == 0:
        return sign + str(n)
    digits = str(n)
    if len(digits) <= places:
        digits = "0" * (places - len(digits) + 1) + digits
    return sign + digits[:-places] + "." + digits[-places:]


def format_units(milliunits, places=2):
    """Format insulin milliunits as units, e.g. 1250 -> '1.25'."""
    return format_decimal(Q(milliunits, MU_PER_UNIT), places)


def floor_to_increment(amount, increment):
    """Largest multiple of `increment` that is <= amount (amount may be Q or int). Never negative."""
    whole = as_q(amount).floor()
    if whole <= 0:
        return 0
    return (whole // increment) * increment


def ceil_div(a, b):
    return -((-a) // b)
