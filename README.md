# GridWise LLM — Smart Campus Energy Optimization

BUP CSE Fest 2026 Hackathon (Online Preliminary) — LLM-assisted operator directive
interpretation + 24-hour campus energy scheduling.

Pipeline:

```text
Energy Data + Operator Notes → LLM Interpreter → Guardrail Validator
    → Math Optimizer → Final Validator → API Response
```

Human notes are never trusted as math directly — they are converted to a fixed
structured format, checked by deterministic guardrails, and only then applied
to the optimizer.

## 0. Submission — judge quick links

| Item | Value |
|---|---|
| Live API | `https://gridwise-llm-a8o9.onrender.com` (`GET /health`, `POST /optimize-energy`) |
| Docker fallback (public) | `fahim3101/gridwise-llm:latest` — Hub page: `https://hub.docker.com/r/fahim3101/gridwise-llm` |
| Image digest (exact artifact) | `sha256:a82cc861ec64f8c3b0f1b35a1272d480aff2cc9ccf3cb0eb0e3f353f8a7e84a1` |
| GitHub | `https://github.com/fahim3101/BUP-Hackathon-Preli` (public after deadline) |
| Solution video (≤ 3 min) | `https://drive.google.com/file/d/1XQ0leikAvxpDJJkT_5cacG11y8KuSPNq/view?usp=sharing` |

## 1. Quickstart — from a clean machine

```bash
git clone https://github.com/fahim3101/BUP-Hackathon-Preli.git
cd BUP-Hackathon-Preli
python -m pip install -r requirements.txt

# optional: API keys for the LLM path (see section 3). Without keys the
# service still works via the deterministic rule-parser fallback.
# Windows:  copy .env.example .env
# Linux:    cp .env.example .env
# then fill in GEMINI_API_KEY (never commit real keys)

# start the service
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Run one public sample end-to-end (`sample01.json` is gitignored, so generate
it from the tracked sample pack — works on a clean clone):

```bash
python -c "import json; d=json.load(open('BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json')); json.dump(d['cases'][0]['input'], open('sample01.json','w'), indent=1)"
curl -X POST http://localhost:8000/optimize-energy -H "Content-Type: application/json" -d @sample01.json
```

Same test against the live judge endpoint (from outside your network):

```bash
curl https://gridwise-llm-a8o9.onrender.com/health
curl -X POST https://gridwise-llm-a8o9.onrender.com/optimize-energy -H "Content-Type: application/json" -d @sample01.json
```

PowerShell equivalent:

```powershell
$body = Get-Content sample01.json -Raw
Invoke-RestMethod -Uri http://localhost:8000/optimize-energy -Method Post -ContentType "application/json" -Body $body
```

## 2. Sample request / response (trimmed)

Request — one scenario object, 24 hourly entries, 1–3 operator notes:

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline."
  ],
  "hours": [
    {"hour": 0, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
    {"hour": 1, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 6}
  ],
  "battery": {
    "capacity_kwh": 220,
    "initial_energy_kwh": 110,
    "minimum_energy_kwh": 40,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 50
  }
}
```

Response — interpretation + 24-hour plan + recomputed totals:

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
      "explanation": "Usable solar is reduced during the stated window."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect the 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 40.0, "solar_used_kwh": 0.0, "battery_action": "discharge", "battery_kwh": 50.0, "battery_energy_after_kwh": 60.0}
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 187.5,
  "plan_summary": "Applied solar_reduction [12, 13]. Minimized grid cost with battery arbitrage while restoring initial energy. Cost BDT 38365.00."
}
```

(`hourly_plan` above shows hour 0 only; the real response contains all 24 hours.
Totals are always recomputed from `hourly_plan`, which is the source of truth.)

## 3. LLM role (mandatory requirement)

* The language model interprets `operator_notes` into `directive_interpretation`
  — and that structured output is what feeds the optimizer constraints.
* The LLM is **not** used only for `plan_summary` / docs (that alone would fail
  the mandatory requirement — see `app/interpreter.py:interpret_notes`).
* Providers, tried in auto-fallback order **Gemini → OpenAI → Groq**, via
  stdlib `urllib` only (no extra SDK dependency):

| Env var | Meaning |
|---|---|
| `GEMINI_API_KEY` | Google Gemini (recommended, free tier) |
| `OPENAI_API_KEY` | OpenAI fallback (`gpt-4o-mini` default) |
| `GROQ_API_KEY` | Groq fallback (`llama-3.3-70b-versatile` default) |
| `LLM_MODEL` / `GEMINI_MODEL` / `OPENAI_MODEL` / `GROQ_MODEL` | Model-name override (see `.env.example`) |
| `LLM_DISABLED=1` | Force the offline rule-based path (local tests / CI) |

* The prompt pins the model to the 6 allowed directive types, the whole-hour
  convention (start-inclusive / end-exclusive, e.g. 1 PM–3 PM → `[13,14]`),
  solar factor = **remaining** fraction (`80% reduction` → `0.2`), `% of
  capacity → kWh` conversion, and `no_op` semantics for irrelevant notes.
* Resilience: if no key is set, the LLM times out (>10 s), returns malformed
  JSON, or fails guardrails, **each note falls back individually** to the
  deterministic rule parser (`app/rule_parser.py`). The service never crashes
  and always answers within the 30 s judge limit (local p95 ≈ 0.07 s).

## 4. Guardrails — deterministic (`app/interpreter.py`)

Every LLM entry passes `guardrail_entry` before it can touch the optimizer:

* `directive_type` must be one of the 6 supported values; `note_index` must
  cover `0..N-1` exactly once, returned in order.
* `no_op` ⇔ `applies=false` + `structured_adjustment=null`; every other
  directive ⇔ `applies=true` + the exact required adjustment shape.
* `hours`: unique integers `0..23`, ascending.
* `solar_reduction.factor` ∈ [0,1]; `minimum_energy_kwh` ∈ [0, capacity];
  `max_grid_kwh` ≥ 0 and finite.
* Invalid LLM entries are discarded **per-note** and replaced by the rule
  parser — one bad note can never poison the other notes.

## 5. Optimizer / solver (`app/optimizer.py`)

Directives change the math deterministically:

| Directive | Effect |
|---|---|
| `solar_reduction` | `effective_solar[h] = solar[h] × factor` |
| `minimum_battery_reserve` | `E_after[h] ≥ max(base minimum, directive minimum)` |
| `no_charge_window` | charge amount `= 0` in those hours |
| `no_discharge_window` | discharge amount `= 0` in those hours |
| `max_grid_window` | `grid[h] ≤ max_grid_kwh` |
| `no_op` | no change |

Solver chain for `min Σ grid[h] × tariff[h]`:

1. **Primary — LP via `scipy.optimize.linprog` (HiGHS):** exact optimum over
   charge / discharge / solar-used / grid / battery-energy variables, subject
   to energy balance, battery transitions, capacity + reserve bounds, hourly
   rate limits, solar caps, grid caps, and end-of-day neutrality
   (`E_after[23] = initial_energy`). Simultaneous charge/discharge is
   canonicalised away (never beneficial at positive tariffs).
2. **Fallback — pure-Python DP** (1 kWh levels anchored at `initial_energy`,
   maximal solar use, all windows/caps respected). No extra dependency;
   guarantees a valid schedule if LP is missing or infeasible.
3. **Last resort — base-valid schedule** ignoring directives, so the API still
   returns HTTP 200 instead of 500.

Totals (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`) are recomputed
from `hourly_plan` after optimisation.

## 6. API contract

* `GET /health` → HTTP 200 `{"status":"ok"}` (ready within 60 s of start).
* `POST /optimize-energy` → request/response exactly per the Problem
  Statement; `scenario_id` is echoed back.
* Error codes: malformed JSON / structural errors → `400`; semantically
  invalid but well-formed requests (negative values, `minimum > capacity`)
  → `422`; internal errors → `500` with a generic message (no secrets or
  stack traces — see `app/main.py`).

## 7. Public-sample test

```bash
python test_samples.py
# expected:
# health OK
# SAMPLE-01: OK  replay-valid cost=38365.0 ref=38365 ratio=1.0000
# ... (all 10 cases OK)
# FAILURES: 0
```

`test_samples.py` POSTs all 10 public cases, replays every `hourly_plan`
against the interpreted directives + battery / energy-balance / neutrality
rules, and compares interpretation semantics (hours + numeric values).
Current status: **10/10 replay-valid, cost ratio 1.0000 vs reference.**

## 8. Docker fallback image

Pull and run the exact submitted artifact
(Hub page: `https://hub.docker.com/r/fahim3101/gridwise-llm`):

```bash
docker pull fahim3101/gridwise-llm:latest
# digest-pinned form:
# fahim3101/gridwise-llm@sha256:a82cc861ec64f8c3b0f1b35a1272d480aff2cc9ccf3cb0eb0e3f353f8a7e84a1
docker run -p 8000:8000 -e GEMINI_API_KEY=... fahim3101/gridwise-llm:latest
curl http://localhost:8000/health
# {"status":"ok"}
```

Build from source (same `Dockerfile` the judges can use):

```bash
docker build -t gridwise-llm:latest .
docker run -p 8000:8000 -e GEMINI_API_KEY=... gridwise-llm:latest
```

The image exposes port `8000`, binds `0.0.0.0`, and contains **no baked-in
secrets** (`.env` is in `.dockerignore`).

## 9. Deployment (live endpoint)

Any reachable platform works. Ours runs on Render:

* Build: `pip install -r requirements.txt`
* Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
* Env: set `GEMINI_API_KEY` on the host (keys stay on the host, never in git)
* Verify from outside your network: `/health` + one sample POST (see
  section 1). Keep quota / rate limits in mind — the rule-parser fallback
  keeps the service valid even if the provider throttles.

## 10. Dependencies & credits

| Package | Use |
|---|---|
| `fastapi` | HTTP API |
| `uvicorn[standard]` | ASGI server |
| `numpy`, `scipy` | LP optimizer (HiGHS via `linprog`) |
| `httpx` | `TestClient` for local tests |

LLM calls use Python stdlib only (`urllib`). No other SDKs. Public
libraries/frameworks are used under their respective open-source licences;
the architecture, guardrails, and optimizer are the team's own work.

## 11. Secret handling

* Real keys live **only** in `.env` (local) or the host's env vars (Render /
  `docker run -e`). `.env` is gitignored **and** dockerignored.
* Never committed: `.env`, `sample01.json` (local scratch), `*.log`.
* API responses and logs never echo keys, tokens, raw prompts containing
  secrets, or stack traces.

## 12. Known limitations

* The DP fallback discretises battery energy to 1 kWh steps (cost error bounded
  by ~1× max tariff vs the LP optimum); LP is used whenever `scipy` is present.
* Bare time ranges without AM/PM (e.g. "one until three") are assumed PM in a
  solar/daytime context — correct under the challenge's whole-hour convention,
  but genuinely ambiguous night windows could misfire; the LLM path resolves
  these when a key is configured.
* No grid export, 100% battery efficiency, and end-of-day neutrality are
  hard-coded per the Problem Statement.

## 13. Repo files

| File | Needed? |
|---|---|
| `app/` (`main.py`, `interpreter.py`, `rule_parser.py`, `optimizer.py`) | Yes — the service |
| `requirements.txt`, `Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example` | Yes — build / run / config template |
| `test_samples.py` | Yes — local validator (`python test_samples.py`) |
| `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` | **Yes — keep.** `test_samples.py` loads it; deleting breaks local reproduction |
| `BUP_CSE_FEST_2026_Preliminary_Problem_Statement_*.md`, `BUP_CSE_FEST_2026_Participant_Guide_*.md` | Reference only — harmless to keep, safe to delete if you want a slimmer repo |
