"""Deterministic rule-based operator-note parser.

Used as (a) fallback when the LLM is unavailable/slow/invalid and
(b) repair/validation helper. Robust to paraphrases via keyword sets,
whole-hour time normalisation and careful numeric extraction.
"""
import re

WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}

ENERGY_KEYWORDS = [
    "solar", "pv", "photovoltaic", "panel", "rooftop", "sun",
    "battery", "charg", "discharg", "grid", "feeder", "transformer",
    "substation", "reserve", "import", "intake", "kwh", "kw",
    "tariff", "inverter", "emergency", "backup",
]


def _replace_word_numbers(text: str) -> str:
    out = text
    # longest first to avoid partial overlap
    for w in sorted(WORD_NUM, key=len, reverse=True):
        out = re.sub(rf"\b{w}\b", str(WORD_NUM[w]), out, flags=re.IGNORECASE)
    return out


def parse_time_window(note: str):
    """Return sorted unique whole hours [start, end) or None."""
    t = note.lower()
    t = t.replace("–", "-").replace("—", "-")
    t = re.sub(r"\bnoon\b", "12 pm", t)
    t = re.sub(r"\bmidday\b", "12 pm", t)
    t = re.sub(r"\bmidnight\b", "12 am", t)
    t = _replace_word_numbers(t)

    # find numeric candidates; skip quantities (% / kwh / kw / bdt)
    pat = re.compile(r"(\d{1,2})(?::(\d{1,2}))?\s*(am|pm|a\.m\.|p\.m\.)?")
    cands = []  # (hour24_or_None, has_marker, raw_h, raw_min, marker, pos)
    for m in pat.finditer(t):
        raw_h = int(m.group(1))
        raw_min = int(m.group(2)) if m.group(2) else 0
        marker = (m.group(3) or "").replace(".", "")
        end = m.end()
        nxt = t[end:end + 8]
        if "%" in nxt[:4] or "kwh" in nxt[:6] or re.match(r"\s*kw\b", nxt):
            continue
        if "percent" in nxt[:10]:
            continue
        # minutes must be < 60; hours sanity
        if raw_min >= 60:
            continue
        if not marker and ":" not in m.group(0):
            # bare number: keep only if near time context
            window = 14
            ctx = t[max(0, m.start() - window): end + window]
            if not re.search(r"from|between|until|till|\bto\b|through|and|-|hour|morning|afternoon|evening|night|am|pm", ctx):
                continue
            if raw_h > 23:
                continue
        else:
            if raw_h > 23:
                continue
        cands.append({"raw_h": raw_h, "raw_min": raw_min, "marker": marker,
                      "has_colon": ":" in m.group(0), "pos": m.start()})
    if len(cands) < 2:
        return None
    # take first two candidates in textual order
    cands = sorted(cands, key=lambda c: c["pos"])[:2]
    a, b = cands[0], cands[1]

    # propagate am/pm when one side misses it ("1-3 pm")
    if not a["marker"] and b["marker"] and not a["has_colon"] and a["raw_h"] <= 12:
        a["marker"] = b["marker"]
    if not b["marker"] and a["marker"] and not b["has_colon"] and b["raw_h"] <= 12:
        # "11 am until 1" -> likely pm for the second if it would otherwise go backwards
        pass  # handled by end<=start fix below

    def to24(c, default_pm_context=False):
        h, marker, has_colon = c["raw_h"], c["marker"], c["has_colon"]
        if marker == "am":
            return 0 if h == 12 else h
        if marker == "pm":
            return 12 if h == 12 else (h + 12 if h < 12 else h)
        if has_colon:
            return h  # 24h clock
        # bare number
        if default_pm_context and 1 <= h <= 11:
            return h + 12
        return h

    solar_ctx = any(k in t for k in ["solar", "pv", "panel", "rooftop", "inverter", "photovoltaic"])
    pm_hint = any(k in t for k in ["afternoon", "evening", "pm", "p.m."])
    am_hint = "morning" in t
    bare_pm = solar_ctx or pm_hint
    if am_hint and not pm_hint:
        bare_pm = False
    s = to24(a, default_pm_context=bare_pm)
    e = to24(b, default_pm_context=True if (bare_pm or b["raw_h"] <= 12 and s >= 11) else bare_pm)
    # fix "11 am until 1" style
    if e <= s and not b["marker"] and b["raw_h"] <= 12 and e + 12 <= 24 and e + 12 > s:
        e = e + 12
    if e <= s or s < 0 or e > 24 or s > 23:
        return None
    s = max(0, min(23, s))
    e = max(1, min(24, e))
    hours = list(range(s, e))
    hours = sorted(set(h for h in hours if 0 <= h <= 23))
    return hours or None


def extract_solar_factor(note: str):
    t = note.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", t)
    if m:
        p = float(m.group(1)) / 100.0
        # "80% reduction" / "reduced by" / "cut by" => remaining = 1-p
        if re.search(r"reduc|cut\s+by|drop\s+by|reduced\s+by|decrease", t):
            return max(0.0, min(1.0, 1.0 - p))
        return max(0.0, min(1.0, p))
    # word fractions
    if re.search(r"one[\s-]*fifth|1[\s-]*fifth|\bfifth\b", t):
        return 0.2
    if re.search(r"one[\s-]*quarter|1[\s-]*quarter|\bquarter\b", t):
        return 0.25
    if re.search(r"one[\s-]*third|1[\s-]*third|\bthird\b", t):
        return 1.0 / 3.0
    if re.search(r"one[\s-]*half|1[\s-]*half|\bhalf\b", t):
        return 0.5
    if re.search(r"\bthree[\s-]*quarter|3/4", t):
        return 0.75
    return None


def extract_kwh(note: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", note.lower())
    if m:
        return float(m.group(1))
    return None


def extract_reserve_kwh(note: str, capacity: float):
    t = note.lower()
    kw = extract_kwh(note)
    if kw is not None:
        return kw
    m = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", t)
    if m and ("capacity" in t or "battery" in t):
        return float(m.group(1)) / 100.0 * capacity
    if "half" in t and ("capacity" in t or "battery" in t):
        return 0.5 * capacity
    if "quarter" in t and ("capacity" in t or "battery" in t):
        return 0.25 * capacity
    if "third" in t and ("capacity" in t or "battery" in t):
        return capacity / 3.0
    if "full" in t and "capacity" in t:
        return float(capacity)
    return None


def is_distractor(note: str) -> bool:
    t = note.lower()
    return not any(k in t for k in ENERGY_KEYWORDS)


def rule_parse_note(note: str, capacity: float):
    """Return (directive_type, structured_adjustment). Never raises."""
    t = note.lower()
    hours = parse_time_window(note)

    has_solar = any(k in t for k in ["solar", "pv", "photovoltaic", "panel", "rooftop", "inverter"])
    has_battery = "battery" in t or "charg" in t or "discharg" in t
    has_grid = any(k in t for k in ["grid", "feeder", "transformer", "substation", "import", "intake"])
    neg = any(k in t for k in ["not ", "n't", "no ", "never", "disabled", "unavailable",
                               "isolated", "must not", "cannot", "can't", "prohibit",
                               "outage", "maintenance", "isolated", "inspect"])
    has_discharge_word = "discharg" in t
    has_charge_word = "charg" in t

    # --- max_grid_window ---
    if has_grid and re.search(r"not exceed|must stay|stay at|or below|at or below|capped|cap\b|limit|maximum|at most|no more than", t):
        cap = extract_kwh(note)
        if cap is not None and hours:
            return "max_grid_window", {"hours": hours, "max_grid_kwh": cap}

    # --- minimum_battery_reserve ---
    if re.search(r"reserve|keep at least|keep\b.*battery|stored|remain\b.*battery|emergency|backup|requires? at least|at least\b.*\bkwh\b", t) and ("battery" in t or "reserve" in t or "emergency" in t or "backup" in t):
        rv = extract_reserve_kwh(note, capacity)
        if rv is not None and hours:
            rv = max(0.0, min(float(capacity), rv))
            return "minimum_battery_reserve", {"hours": hours, "minimum_energy_kwh": rv}

    # --- no_discharge_window (check before charge: 'discharge' contains 'charge' substring risk avoided by explicit word) ---
    if has_discharge_word and (neg or re.search(r"must not|do not|don't|no .*discharg|without.*discharg|disabled|unavailable|prohibit|protection|testing|relay", t)):
        if hours:
            return "no_discharge_window", {"hours": hours}

    # --- no_charge_window ---
    if has_charge_word and not has_discharge_word and (neg or re.search(r"unavailable|disabled|isolated|outage|maintenance|inspect|must not|do not|don't|no charg", t)):
        if hours:
            return "no_charge_window", {"hours": hours}
    # charger isolated phrasing without explicit 'not'
    if re.search(r"charger.*(isolated|maintenance|inspect|outage|unavailable|disabled)", t) and hours:
        return "no_charge_window", {"hours": hours}
    if re.search(r"charging.*(unavailable|disabled|isolated|outage)", t) and hours:
        return "no_charge_window", {"hours": hours}

    # --- solar_reduction ---
    if has_solar and (re.search(r"reduc|drop|%|percent|fraction|half|quarter|third|fifth|half|clean|wash|inspect|maintenance|inverter|cloud|shade|cover|output|forecast|usable", t)):
        f = extract_solar_factor(note)
        if f is not None and hours:
            return "solar_reduction", {"hours": hours, "factor": f}
        if hours and ("clean" in t or "wash" in t or "maintenance" in t):
            # last-resort factor if wording lacks explicit number
            return "solar_reduction", {"hours": hours, "factor": 0.5}

    # --- fallback heuristics when hours missing but intent clear ---
    # (guardrail requires hours; without hours we cannot emit applicable directive)
    if is_distractor(note):
        return "no_op", None
    # If energy keywords exist but we failed to parse, still return no_op shape
    # and let the LLM path (when available) override. Optimizer stays feasible.
    # To avoid silent wrong no_op, caller prefers LLM result when present.
    return "no_op", None
