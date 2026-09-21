"""LLM interpreter with deterministic guardrails.

Flow: operator notes -> LLM (Gemini / OpenAI / Groq) -> guardrail validator
      -> per-note fallback to rule_parser on any failure.
The LLM emits ONLY language judgments — directive type, windows [start,end)
and value+unit. ALL arithmetic (windows -> hours, % -> factor/kWh) happens in
app/units.py, so LLM arithmetic mistakes cannot leak through.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from .rule_parser import rule_parse_note
from .units import ADJ_KEYS, to_adjustment

ALLOWED = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
           "no_discharge_window", "max_grid_window", "no_op"}
UNIT_ENUM = ("kwh", "percent_remaining", "percent_reduction", "none")
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

_cache: dict = {}


def _env(name, default=""):
    return os.environ.get(name, default)


def llm_enabled() -> bool:
    if _env("LLM_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    return bool(_env("GEMINI_API_KEY") or _env("OPENAI_API_KEY") or _env("GROQ_API_KEY"))


def build_prompt(notes, capacity):
    sys = (
        "You convert campus energy operator notes into structured directives for ONE 24-hour schedule.\n"
        "Return EXACTLY one item per note, in order, note_index starting at 0.\n"
        "directive_type (only these): solar_reduction | minimum_battery_reserve | "
        "no_charge_window | no_discharge_window | max_grid_window | no_op.\n"
        "Meaning: solar_reduction = rooftop solar/PV output lowered; "
        "minimum_battery_reserve = battery must keep stored energy; "
        "no_charge_window = battery cannot be charged; "
        "no_discharge_window = battery cannot discharge; "
        "max_grid_window = grid import per hour capped; "
        "no_op = admin news/events/menus/deadlines, unrelated equipment, or another period (next week/month).\n"
        "windows: list of [start_hour, end_hour) on a 24h clock, start included, end EXCLUDED. "
        '"1 PM to 3 PM" -> [[13,15]]. noon=12. Midnight ending a window -> 24. '
        '"from 6 PM for three hours" -> [[18,21]]. Single hour "at 7 PM" -> [[19,20]]. '
        '"all day" -> [[0,24]]. Overnight "10 PM to 2 AM" -> [[22,2]]. no_op -> [].\n'
        "value + value_unit (NEVER do arithmetic, just report what the note says):\n"
        "- kwh: absolute energy (reserve kWh, grid cap kWh per hour).\n"
        '- percent_remaining: what is LEFT ("drops to 20%" -> 20, "a quarter of forecast" -> 25, '
        '"half" -> 50, "one-fifth" -> 20). Reserve as % of battery capacity also uses this.\n'
        '- percent_reduction: what is CUT ("80% reduction" -> 80, "reduced by three quarters" -> 75).\n'
        "- none (value 0): charge/discharge windows and no_op.\n"
        "Never invent numbers; use only stated or clearly implied quantities.\n"
        f"Battery capacity is {capacity} kWh (for context only; report percentages as percentages).\n"
        "Return ONLY a JSON array: "
        '[{"note_index":0,"directive_type":"...","windows":[[s,e]],"value":number,'
        '"value_unit":"kwh|percent_remaining|percent_reduction|none","explanation":"..."}].'
    )
    user = {"battery_capacity_kwh": capacity,
            "notes": [{"note_index": i, "text": n} for i, n in enumerate(notes)]}
    return sys, json.dumps(user)


GEMINI_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "note_index": {"type": "integer"},
            "directive_type": {"type": "string", "enum": sorted(ALLOWED)},
            "windows": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
            "value": {"type": "number"},
            "value_unit": {"type": "string", "enum": list(UNIT_ENUM)},
            "explanation": {"type": "string"},
        },
        "required": ["note_index", "directive_type", "windows", "value", "value_unit", "explanation"],
    },
}

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _post_json(url, payload, headers, timeout=8, retries=1):
    data = json.dumps(payload).encode()
    base = {"Content-Type": "application/json", "User-Agent": BROWSER_UA, "Accept": "*/*"}
    base.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=base, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if 500 <= e.code < 600 and attempt < retries:
                time.sleep(1.5)
                continue
            raise
    raise last


def call_gemini(system, user_text, model, timeout=8):
    key = _env("GEMINI_API_KEY")
    model = model or _env("LLM_MODEL") or _env("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"system_instruction": {"parts": [{"text": system}]},
               "contents": [{"parts": [{"text": user_text}]}],
               "generationConfig": {"temperature": 0, "response_mime_type": "application/json",
                                    "response_json_schema": GEMINI_SCHEMA}}
    out = _post_json(url, payload, {"Content-Type": "application/json",
                                    "X-goog-api-key": key}, timeout)
    try:
        return out["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return json.dumps(out)


def call_openai_compatible(system, user_text, base_url, api_key, model, timeout=8):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user_text}],
               "response_format": {"type": "json_object"}}
    out = _post_json(url, payload, {"Content-Type": "application/json",
                                    "Authorization": f"Bearer {api_key}"}, timeout)
    return out["choices"][0]["message"]["content"]


def call_llm(system, user_text, timeout=8):
    """Try providers in order: Gemini -> OpenAI -> Groq."""
    if _env("GEMINI_API_KEY"):
        try:
            return call_gemini(system, user_text, _env("GEMINI_MODEL") or _env("LLM_MODEL") or DEFAULT_GEMINI_MODEL, timeout)
        except Exception:
            pass
    if _env("OPENAI_API_KEY"):
        try:
            return call_openai_compatible(system, user_text, "https://api.openai.com/v1",
                                          _env("OPENAI_API_KEY"),
                                          _env("LLM_MODEL") or _env("OPENAI_MODEL") or "gpt-4o-mini", timeout)
        except Exception:
            pass
    if _env("GROQ_API_KEY"):
        try:
            return call_openai_compatible(system, user_text, "https://api.groq.com/openai/v1",
                                          _env("GROQ_API_KEY"),
                                          _env("LLM_MODEL") or _env("GROQ_MODEL") or "llama-3.3-70b-versatile", timeout)
        except Exception:
            pass
    raise RuntimeError("no LLM available")


def extract_json_array(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, list):
                    return v
            return [obj]
    except Exception:
        pass
    m = re.search(r"\[.*\]", t, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("no JSON array in LLM output")


def _check_intermediate(item) -> None:
    """Structural check of one raw LLM item (untrusted). Raises on failure."""
    if not isinstance(item, dict):
        raise ValueError("entry not object")
    if item.get("directive_type") not in ALLOWED:
        raise ValueError("bad directive_type")
    if item.get("value_unit") not in UNIT_ENUM:
        raise ValueError("bad value_unit")
    v = item.get("value")
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError("bad value")
    if not isinstance(item.get("windows"), list):
        raise ValueError("bad windows")


def to_entry(note_index, dtype, inter, capacity, explanation) -> dict:
    """Convert a checked intermediate item to the exact response entry."""
    exp = (str(explanation or "").strip()[:300]
           or "Interpreted operator note.")
    if dtype == "no_op":
        return {"note_index": note_index, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None, "explanation": exp}
    adj = to_adjustment(dtype, inter, float(capacity))  # raises on any violation
    if set(adj) != ADJ_KEYS[dtype]:
        raise ValueError("bad adjustment shape")
    return {"note_index": note_index, "applies": True, "directive_type": dtype,
            "structured_adjustment": adj, "explanation": exp}


def rule_fallback_entry(idx, note, capacity):
    dt, inter = rule_parse_note(note)
    if dt == "no_op":
        return {"note_index": idx, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "This note does not affect the 24-hour energy schedule."}
    exp_map = {
        "solar_reduction": "Usable solar is reduced during the stated window.",
        "minimum_battery_reserve": "A minimum battery reserve is required during the stated window.",
        "no_charge_window": "Battery charging is unavailable during the stated window.",
        "no_discharge_window": "Battery discharging is unavailable during the stated window.",
        "max_grid_window": "Grid import is capped during the stated window.",
    }
    try:
        return to_entry(idx, dt, inter, float(capacity), exp_map.get(dt, "Applied operator directive."))
    except Exception:
        return {"note_index": idx, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "This note does not affect the 24-hour energy schedule."}


def interpret_notes(notes, capacity):
    """Main entry: LLM first (if configured), guardrails, per-note rule fallback."""
    n = len(notes)
    key = (tuple(notes), round(float(capacity), 3))
    if key in _cache:
        return [dict(e) for e in _cache[key]]

    llm_entries = None
    if llm_enabled():
        try:
            system, user_text = build_prompt(notes, capacity)
            raw = call_llm(system, user_text, timeout=8)
            arr = extract_json_array(raw)
            if isinstance(arr, list) and len(arr) == n:
                llm_entries = arr
        except Exception:
            llm_entries = None

    out = []
    for i, note in enumerate(notes):
        e = None
        if llm_entries is not None:
            try:
                cand = next(x for x in llm_entries if isinstance(x, dict) and x.get("note_index") == i)
                _check_intermediate(cand)
                e = to_entry(i, cand["directive_type"], cand, float(capacity),
                             cand.get("explanation"))
            except Exception:
                e = None
        if e is None:
            e = rule_fallback_entry(i, note, float(capacity))
        out.append(e)
    out = sorted(out, key=lambda x: x["note_index"])
    _cache[key] = [dict(e) for e in out]
    if len(_cache) > 2000:
        _cache.clear()
    return out
