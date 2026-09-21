"""Deterministic rule-based operator-note parser (fallback layer).

Emits an INTERMEDIATE shape — directive type + windows [[start,end)] +
value + value_unit — so all arithmetic (window -> hours, % -> factor/kWh)
happens centrally in app/units.py, never here and never in the LLM.
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
    "battery", "batteries", "stor", "charg", "discharg", "grid",
    "feeder", "transformer", "substation", "reserve", "import",
    "intake", "kwh", "kw", "tariff", "inverter", "emergency",
    "backup", "cushion", "buffer",
]

UNITS = ("kwh", "percent_remaining", "percent_reduction", "none")


def _replace_word_numbers(text: str) -> str:
    out = text
    for w in sorted(WORD_NUM, key=len, reverse=True):
        out = re.sub(rf"\b{w}\b", str(WORD_NUM[w]), out, flags=re.IGNORECASE)
    return out


def _tok_to_24h(raw_h: int, raw_min: int, marker: str, has_colon: bool):
    if marker == "am":
        return (0 if raw_h == 12 else raw_h), "am"
    if marker == "pm":
        return (12 if raw_h == 12 else (raw_h + 12 if raw_h < 12 else raw_h)), "pm"
    if has_colon:
        return raw_h, "24h"
    return raw_h, None


def parse_windows(note: str):
    """Return [[start,end), ...] with end possibly 24 (midnight) and
    start > end meaning overnight wrap. Single-hour notes give [[h,h+1]]."""
    t = note.lower().replace("–", "-").replace("—", "-")
    t = re.sub(r"\bmidday\b", "noon", t)
    t = _replace_word_numbers(t)
    # midnight as a RANGE END means 24; as a start means 0
    t = re.sub(r"\bmidnight\b", "12 am", t)

    pat = re.compile(r"(noon|midnight|\d{1,2})(?::(\d{1,2}))?\s*(am|pm|a\.m\.|p\.m\.)?")
    cands = []
    for m in pat.finditer(t):
        tok, raw_min, mk = m.group(1), m.group(2), (m.group(3) or "").replace(".", "")
        if tok in ("noon", "midnight") and m.start() > 0 and (t[m.start() - 1].isalnum() or t[m.start() - 1] == "_"):
            continue  # e.g. "afternoon" is not noon
        if tok == "noon":
            cands.append({"h": 12, "mer": "pm", "colon": False, "pos": m.start()})
            continue
        if tok == "midnight":
            cands.append({"h": 0, "mer": "am", "colon": False, "mid": True, "pos": m.start()})
            continue
        raw_h, raw_min = int(tok), int(raw_min) if raw_min else 0
        if raw_min >= 60 or raw_h > 23:
            continue
        if raw_h == 0 and not mk and ":" not in m.group(0):
            continue  # ghost from "zero"->0; real midnight comes via the word
        end = m.end()
        nxt = t[end:end + 8]
        if "%" in nxt[:4] or "kwh" in nxt[:6] or re.match(r"\s*kw\b", nxt) or "percent" in nxt[:10]:
            continue  # a quantity, not a time
        if re.match(r"\s*-\s*(fifth|quarter|third|half|fourth)", t[end:end + 12], re.I):
            continue  # "one-fifth" fraction, not a time
        if re.match(r"\s*(quarters?|thirds?|fifths?|hal(f|ves)|fourths?)\b", t[end:end + 12], re.I):
            continue  # "three quarters" fraction, not a time
        has_colon = ":" in m.group(0)
        if not mk and not has_colon:
            ctx = t[max(0, m.start() - 14): end + 14]
            if not re.search(r"from|between|until|till|\bto\b|through|and|-|hour|morning|afternoon|evening|night|am|pm|noon|midnight", ctx):
                continue
        h24, mer = _tok_to_24h(raw_h, raw_min, mk, has_colon)
        cands.append({"h": h24, "mer": mer, "colon": has_colon, "pos": m.start()})

    if not cands:
        return []
    cands = sorted(cands, key=lambda c: c["pos"])

    # single-hour phrasing: "the 3 PM hour", "at 7 PM", "during the 7 AM hour"
    if len(cands) == 1:
        c = cands[0]
        if re.search(r"\bhour\b|\bat\b", t):
            h = c["h"]
            if c["mer"] is None and h < 7 and ("evening" in t or "afternoon" in t or "night" in t):
                h += 12
            return [[h % 24, (h % 24) + 1]]
        return []

    a, b = cands[0], cands[1]
    # "1-3 PM": first inherits PM
    if a["mer"] is None and b["mer"] == "pm" and not a["colon"] and a["h"] <= 12:
        a["h"] = 12 if a["h"] == 12 else a["h"] + 12
        a["mer"] = "pm"
    s = a["h"]
    e = b["h"]
    if b.get("mid"):
        e = 24  # "... until midnight"
    pm_ctx = ("pm" in t or "evening" in t or "afternoon" in t)
    sol_ctx = any(k in t for k in ["solar", "pv", "panel", "rooftop", "inverter", "photovoltaic"])
    # bare numbers in daytime/solar context are PM (decided BEFORE any wrap logic)
    if a["mer"] is None and not a["colon"] and 1 <= s <= 11 and (sol_ctx or pm_ctx):
        s += 12
        a["mer"] = "pm"
    if b["mer"] is None and not b["colon"] and 1 <= e <= 11 and (sol_ctx or pm_ctx or a["mer"] == "pm"):
        e += 12
        b["mer"] = "pm"
    # "11 AM until 1": bare end earlier than start -> assume crosses noon
    if e <= s and b["mer"] is None and not b["colon"] and e + 12 <= 24 and e + 12 > s:
        e += 12
    if not (0 <= s <= 23) or not (0 <= e <= 24):
        return []
    if e == s:
        return [[s, s + 1]] if s < 23 else [[23, 24]]
    return [[s, e]]  # e <= s intentionally kept: overnight wrap, expanded later


def extract_percent(note: str):
    """Return raw percentage number (0-100 scale) or None. Handles digits,
    'percent', halve/halves, nothing/zero, quarter/third/fifth words."""
    t = note.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", t)
    if m:
        return float(m.group(1))
    if re.search(r"\bhalv(e|es|ed|ing)\b|\bhalf\b", t):
        return 50.0
    if re.search(r"\bnothing\b|\bno\b.{0,12}\boutput\b|\bzero\b", t):
        return 0.0
    if re.search(r"one[\s-]*(fifth|quarter|third)|a[\s-]*(fifth|quarter|third)|\b(fifth|quarter|third)\b", t):
        if "fifth" in t:
            return 20.0
        if "quarter" in t:
            return 25.0
        return 100.0 / 3.0
    if re.search(r"three[\s-]*quarters?", t):
        return 75.0
    if re.search(r"two[\s-]*thirds?", t):
        return 200.0 / 3.0
    return None


def extract_kwh(note: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", note.lower())
    return float(m.group(1)) if m else None


def _solar_unit(t: str) -> str:
    # "reduced by X" / "cutting ... by X" / "fall by X" / "X% reduction" = CUT.
    # "drop to X" / "reduce to X" / "of forecast" = what is LEFT.
    if re.search(r"\bby\b\s*(\d|three|quarter|half|third|fifth)", t) and re.search(
            r"reduc|cut|drop|lower|decrease|fall|los|shav|curtail|slash|trim", t):
        return "percent_reduction"
    if re.search(r"\breduction\b|\bcurtail\w*", t):
        return "percent_reduction"
    return "percent_remaining"


def is_distractor(note: str) -> bool:
    return not any(k in note.lower() for k in ENERGY_KEYWORDS)


def rule_parse_note(note: str):
    """Return (directive_type, intermediate|None). Intermediate shape:
    {"windows": [[s,e],...], "value": number, "value_unit": unit}.
    Never raises; no capacity needed (conversion is centralized)."""
    t = note.lower()
    windows = parse_windows(note)

    has_solar = any(k in t for k in ["solar", "pv", "photovoltaic", "panel", "rooftop", "inverter", "sun", "array"])
    has_grid = any(k in t for k in ["grid", "feeder", "transformer", "substation", "import",
                                    "intake", "purchase", "utility", "demand response", "mains"])
    # --- max_grid_window ---
    if has_grid and re.search(r"not exceed|must stay|stay at|stay under|or below|at or below|capp?ed|\bcap\b|ceiling|limit|maximum|at most|no more than|keep.{0,20}(below|under)", t):
        kw = extract_kwh(note)
        if kw is not None and windows:
            return "max_grid_window", {"windows": windows, "value": kw, "value_unit": "kwh"}

    # --- minimum_battery_reserve ---
    reserve_ctx = re.search(r"reserve|keep|retain|hold|maintain|at least|remain|minimum|never drop|drop (under|below)|fall (under|below)|below|under|cushion|buffer|backup|emergency|requires?", t)
    batt_ctx = any(k in t for k in ["batter", "stor", "capacity", "reserve", "emergency", "backup", "cell", "pack"])
    if reserve_ctx and batt_ctx:
        kw = extract_kwh(note)
        if kw is not None and windows:
            return "minimum_battery_reserve", {"windows": windows, "value": kw, "value_unit": "kwh"}
        pct = extract_percent(note)
        if pct is not None and windows and any(k in t for k in ["capacity", "batter", "cell", "pack", "stor"]):
            return "minimum_battery_reserve", {"windows": windows, "value": pct, "value_unit": "percent_remaining"}

    # --- no_discharge_window ---
    dis_block = ("discharg" in t) or (
        re.search(r"suppl|deliver|provid|export|feed\b|output.{0,12}block", t)
        and (any(k in t for k in ["batter", "stor", "campus", "system"]) or "not" in t or "n't" in t))
    if dis_block and re.search(r"not |n't|no |never|block|must not|cannot|can't|prohibit|forbidden|unavailable|disabled|testing|relay|protection|check|safety", t):
        if windows:
            return "no_discharge_window", {"windows": windows, "value": 0, "value_unit": "none"}

    # --- no_charge_window ---
    ch_block = ("charg" in t) or re.search(r"accept.{0,12}energy|lock out.{0,12}charger|charger.{0,12}(lock|isolated|offline)", t)
    if ch_block and "discharg" not in t and re.search(
            r"not |n't|no |never|prohibit|forbid|must not|cannot|can't|unavailable|disabled|isolated|offline|outage|maintenance|inspect|lock|replace|update|test|block|servic|dead|dark", t):
        if windows:
            return "no_charge_window", {"windows": windows, "value": 0, "value_unit": "none"}

    # --- solar_reduction ---
    if has_solar and re.search(r"reduc|drop|cut|shade|cloud|dust|haze|halv|half|nothing|zero|%|percent|fraction|quarter|third|fifth|clean|wash|inspect|maintenance|inverter|cover|output|forecast|usable|offline|yield|produce|shading|monsoon|scaffold|curtail|eclipse|fog|storm|trim|dim|blot|sink|slash", t):
        pct = extract_percent(note)
        if pct is not None and windows:
            return "solar_reduction", {"windows": windows, "value": pct, "value_unit": _solar_unit(t)}

    return "no_op", None
