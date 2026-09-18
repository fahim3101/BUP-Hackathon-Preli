"""LLM interpreter with deterministic guardrails.

Flow: operator notes -> LLM (Gemini / OpenAI / Groq) -> guardrail validator
      -> per-note fallback to rule_parser on any failure.
The LLM is always attempted first when credentials exist; otherwise the
rule parser provides a fully offline, deterministic result so local
reproduction and judging never crash.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

from .rule_parser import rule_parse_note, parse_time_window

ALLOWED = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
           "no_discharge_window", "max_grid_window", "no_op"}

_cache: dict = {}


def _env(name, default=""):
    return os.environ.get(name, default)


def llm_enabled() -> bool:
    if _env("LLM_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    return bool(_env("GEMINI_API_KEY") or _env("OPENAI_API_KEY") or _env("GROQ_API_KEY"))


def build_prompt(notes, capacity):
    sys = (
        "You are GridWise, a smart-campus energy operator-note interpreter.\n"
        "Convert EACH operator note into exactly ONE structured directive.\n"
        "Supported directive_type values (only these):\n"
        '- solar_reduction: usable solar fraction remains. structured_adjustment={"hours":[...], "factor":0..1}\n'
        '- minimum_battery_reserve: battery energy floor. {"hours":[...], "minimum_energy_kwh":number}\n'
        '- no_charge_window: charging forbidden. {"hours":[...]}\n'
        '- no_discharge_window: discharging forbidden. {"hours":[...]}\n'
        '- max_grid_window: grid import cap per hour. {"hours":[...], "max_grid_kwh":number}\n'
        '- no_op: note irrelevant to this 24h energy schedule. structured_adjustment=null\n'
        "Rules:\n"
        "- hours are whole hours 0-23, start-INCLUSIVE end-EXCLUSIVE, ascending unique. "
        "1 PM to 3 PM -> [13,14]. noon=12, midnight=0. 13:00-15:00 -> [13,14].\n"
        "- solar factor = usable fraction REMAINING (0-1). '80% reduction' -> 0.2. 'drop to 20%' -> 0.2. '25% of forecast' -> 0.25. 'about half' -> 0.5. 'one-fifth' -> 0.2.\n"
        f"- battery capacity is {capacity} kWh. Convert percentages: '50% of capacity' -> {0.5 * float(capacity)}.\n"
        "- cafeteria/menu/sports/registration/library/seminar/club notices with no energy content -> no_op.\n"
        "- Return ONLY a JSON array with one object per note in order: "
        '[{"note_index":0,"applies":true,"directive_type":"...","structured_adjustment":{...}|null,"explanation":"..."}]. '
        "applies=false only for no_op."
    )
    user = {"battery_capacity_kwh": capacity,
            "notes": [{"note_index": i, "text": n} for i, n in enumerate(notes)]}
    return sys, json.dumps(user)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _post_json(url, payload, headers, timeout=12, retries=1):
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


def call_gemini(system, user_text, model, timeout=12):
    key = _env("GEMINI_API_KEY")
    model = model or _env("LLM_MODEL") or _env("GEMINI_MODEL") or "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"system_instruction": {"parts": [{"text": system}]},
               "contents": [{"parts": [{"text": user_text}]}],
               "generationConfig": {"temperature": 0, "response_mime_type": "application/json"}}
    out = _post_json(url, payload, {"Content-Type": "application/json",
                                    "X-goog-api-key": key}, timeout)
    try:
        return out["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return json.dumps(out)


def call_openai_compatible(system, user_text, base_url, api_key, model, timeout=12):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user_text}],
               "response_format": {"type": "json_object"}}
    out = _post_json(url, payload, {"Content-Type": "application/json",
                                    "Authorization": f"Bearer {api_key}"}, timeout)
    return out["choices"][0]["message"]["content"]


def call_llm(system, user_text, timeout=10):
    """Try providers in order: Gemini -> OpenAI -> Groq."""
    if _env("GEMINI_API_KEY"):
        try:
            return call_gemini(system, user_text, _env("GEMINI_MODEL") or _env("LLM_MODEL") or "gemini-2.0-flash", timeout)
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
    # direct parse
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


def guardrail_entry(e, n_notes, capacity):
    """Validate+normalise one LLM entry. Returns normalised dict or raises."""
    if not isinstance(e, dict):
        raise ValueError("entry not object")
    ni = e.get("note_index")
    if not isinstance(ni, int) or not (0 <= ni < n_notes):
        raise ValueError("bad note_index")
    dt = e.get("directive_type")
    if dt not in ALLOWED:
        raise ValueError("bad directive_type")
    applies = e.get("applies")
    adj = e.get("structured_adjustment")
    exp = str(e.get("explanation", "") or "")[:300] or "Interpreted operator note."
    if dt == "no_op":
        if applies is not False:
            raise ValueError("no_op must have applies=false")
        if adj is not None:
            raise ValueError("no_op adjustment must be null")
        return {"note_index": ni, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None, "explanation": exp}
    if applies is not True:
        raise ValueError("applicable directive must have applies=true")
    if not isinstance(adj, dict):
        raise ValueError("adjustment must be object")
    hours = adj.get("hours")
    if not isinstance(hours, list) or not hours:
        raise ValueError("hours missing")
    if any(not isinstance(h, int) or not (0 <= h <= 23) for h in hours):
        raise ValueError("hour out of range")
    if len(set(hours)) != len(hours):
        raise ValueError("duplicate hours")
    hours = sorted(set(hours))
    if dt == "solar_reduction":
        f = adj.get("factor")
        if not isinstance(f, (int, float)) or not (0 <= f <= 1):
            raise ValueError("bad factor")
        if set(adj.keys()) - {"hours", "factor"}:
            pass
        return {"note_index": ni, "applies": True, "directive_type": dt,
                "structured_adjustment": {"hours": hours, "factor": float(f)},
                "explanation": exp}
    if dt == "minimum_battery_reserve":
        v = adj.get("minimum_energy_kwh")
        if not isinstance(v, (int, float)) or not (0 <= v <= capacity):
            raise ValueError("bad reserve")
        return {"note_index": ni, "applies": True, "directive_type": dt,
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": float(v)},
                "explanation": exp}
    if dt in ("no_charge_window", "no_discharge_window"):
        return {"note_index": ni, "applies": True, "directive_type": dt,
                "structured_adjustment": {"hours": hours}, "explanation": exp}
    if dt == "max_grid_window":
        v = adj.get("max_grid_kwh")
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError("bad grid cap")
        return {"note_index": ni, "applies": True, "directive_type": dt,
                "structured_adjustment": {"hours": hours, "max_grid_kwh": float(v)},
                "explanation": exp}
    raise ValueError("unknown directive")


def rule_fallback_entry(idx, note, capacity):
    dt, adj = rule_parse_note(note, capacity)
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
    return {"note_index": idx, "applies": True, "directive_type": dt,
            "structured_adjustment": adj, "explanation": exp_map.get(dt, "Applied operator directive.")}


def interpret_notes(notes, capacity):
    """Main entry: LLM first (if configured), guardrails, per-note rule fallback."""
    n = len(notes)
    # cache key includes capacity bucket to handle % conversions
    key = (tuple(notes), round(float(capacity), 3))
    if key in _cache:
        return [dict(e) for e in _cache[key]]

    llm_entries = None
    if llm_enabled():
        try:
            system, user_text = build_prompt(notes, capacity)
            t0 = time.time()
            raw = call_llm(system, user_text, timeout=10)
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
                e = guardrail_entry(cand, n, float(capacity))
            except Exception:
                e = None
        if e is None:
            e = rule_fallback_entry(i, note, float(capacity))
            # final safety: validate fallback too
            try:
                e = guardrail_entry(e, n, float(capacity))
            except Exception:
                e = {"note_index": i, "applies": False, "directive_type": "no_op",
                     "structured_adjustment": None,
                     "explanation": "This note does not affect the 24-hour energy schedule."}
        out.append(e)
    out = sorted(out, key=lambda x: x["note_index"])
    _cache[key] = [dict(e) for e in out]
    # bound cache
    if len(_cache) > 2000:
        _cache.clear()
    return out
