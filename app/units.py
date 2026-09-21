"""Central arithmetic for directives (single place of truth).

The LLM and the rule parser both emit the intermediate shape:
    {"windows": [[start,end), ...], "value": number, "value_unit": unit}
with units kwh | percent_remaining | percent_reduction | none.
Only this module converts windows -> hours and units -> factor/kWh, so
off-by-one and 1-x mistakes cannot leak in from language understanding.
"""
import math

ADJ_KEYS = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


def expand_windows(windows) -> list:
    """[[start,end)] on a 24h clock. end==24 is midnight. start>end wraps
    past midnight. start==end is a single hour. Returns sorted unique 0..23."""
    if not isinstance(windows, list):
        raise ValueError("windows must be a list")
    hours = set()
    for w in windows:
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            raise ValueError("window must be [start,end]")
        s, e = w
        if isinstance(s, float) and float(s).is_integer():
            s = int(s)
        if isinstance(e, float) and float(e).is_integer():
            e = int(e)
        if not isinstance(s, int) or isinstance(s, bool) or not 0 <= s <= 23:
            raise ValueError("start_hour outside 0..23")
        if not isinstance(e, int) or isinstance(e, bool) or not 0 <= e <= 24:
            raise ValueError("end_hour outside 0..24")
        if s == e:
            hours.add(s)
        elif s < e:
            hours.update(range(s, e))
        else:  # overnight wrap
            hours.update(range(s, 24))
            hours.update(range(0, e))
    out = sorted(hours)
    if not out:
        raise ValueError("empty hours")
    return out


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError("value must be a finite number")
    return float(v)


def factor_from(value, unit) -> float:
    """Usable solar fraction remaining in 0..1."""
    if unit not in ("percent_remaining", "percent_reduction"):
        raise ValueError("solar_reduction needs a percentage unit")
    v = _num(value)
    if not 0 <= v <= 100:
        raise ValueError("percentage outside 0..100")
    frac = v / 100.0
    return frac if unit == "percent_remaining" else 1.0 - frac


def kwh_from(value, unit, capacity) -> float:
    if unit == "kwh":
        return _num(value)
    if unit == "percent_remaining":
        v = _num(value)
        if not 0 <= v <= 100:
            raise ValueError("percentage outside 0..100")
        return min(float(capacity), float(capacity) * v / 100.0)
    raise ValueError("reserve needs kwh or percent_remaining")


def to_adjustment(dtype, inter, capacity) -> dict:
    """Build the exact structured_adjustment for a directive type."""
    if not isinstance(inter, dict):
        raise ValueError("adjustment must be an object")
    hours = expand_windows(inter.get("windows"))
    value, unit = _num(inter.get("value", 0)), inter.get("value_unit")
    if dtype == "solar_reduction":
        f = factor_from(value, unit)
        if not 0 <= f <= 1:
            raise ValueError("factor outside 0..1")
        return {"hours": hours, "factor": f}
    if dtype == "minimum_battery_reserve":
        rv = kwh_from(value, unit, capacity)
        if not 0 <= rv <= capacity:
            raise ValueError("reserve outside 0..capacity")
        return {"hours": hours, "minimum_energy_kwh": rv}
    if dtype == "max_grid_window":
        if unit != "kwh":
            raise ValueError("grid cap needs kwh")
        cap = _num(value)
        if cap < 0:
            raise ValueError("grid cap negative")
        return {"hours": hours, "max_grid_kwh": cap}
    if dtype in ("no_charge_window", "no_discharge_window"):
        return {"hours": hours}
    raise ValueError("unsupported directive_type")
