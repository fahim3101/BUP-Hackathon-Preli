# GridWise LLM — Smart Campus Energy Optimization

BUP CSE Fest 2026 Hackathon (Online Preliminary) — LLM-assisted operator directive interpretation + 24h energy scheduling.

Architecture: `Energy Data + Operator Notes → LLM Interpreter → Guardrail Validator → Math Optimizer → Final Validator → API Response`

## 1. Quickstart (clean environment)

```bash
python -m pip install -r requirements.txt
# offline deterministic check (no key needed)
set LLM_DISABLED=1
python test_samples.py
# start service
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Optimize (SAMPLE-01 input in `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`):

```bash
curl -X POST http://localhost:8000/optimize-energy -H "Content-Type: application/json" -d @sample01.json
```

With PowerShell:

```powershell
$body = Get-Content sample01.json -Raw
Invoke-RestMethod -Uri http://localhost:8000/optimize-energy -Method Post -ContentType "application/json" -Body $body
```

## 2. LLM role (mandatory requirement)

* The language model interprets `operator_notes` into `directive_interpretation` — this output feeds the optimizer constraints.
* LLM is **not** used only for `plan_summary`/docs.
* Providers (auto-fallback order): **Gemini → OpenAI → Groq**, via stdlib `urllib` (no extra deps).
* Env vars (see `.env.example`, never commit real keys):

| Var | Meaning |
|---|---|
| `GEMINI_API_KEY` | Google Gemini (recommended, free tier). Default model `gemini-flash-latest` |
| `OPENAI_API_KEY` | OpenAI fallback (`gpt-4o-mini`) |
| `GROQ_API_KEY` | Groq fallback (`llama-3.3-70b-versatile`) |
| `LLM_MODEL` / `GEMINI_MODEL` / `OPENAI_MODEL` / `GROQ_MODEL` | Model override |
| `LLM_DISABLED=1` | Force offline rule-based path (local tests/CI) |

* Prompt constrains output to the 6 directive types, whole-hour convention (start-inclusive/end-exclusive), solar factor = remaining fraction, `% of capacity → kWh` conversion, and `no_op` semantics.
* If no key is set, or the LLM times out (>10s), returns malformed JSON, or fails guardrails, each note falls back to the deterministic rule parser — the service **never crashes** and always returns a valid schedule within the 30s limit (local p95 ≈ 0.07s).

## 3. Guardrails (deterministic, `app/interpreter.py`)

* `directive_type` ∈ 6 allowed values; `note_index` covers `0..N-1` exactly once, returned in order.
* `no_op` ⇔ `applies=false` + `adjustment=null`; all others ⇔ `applies=true` + exact shape.
* `hours`: unique ints `0..23`, ascending.
* `solar_reduction.factor` ∈ [0,1]; `minimum_energy_kwh` ∈ [0, capacity]; `max_grid_kwh` ≥ 0.
* Invalid LLM entries are discarded per-note and replaced by the rule parser.

## 4. Optimizer / solver (`app/optimizer.py`)

* Applies directives: `effective_solar = solar × factor`; raised `E_after` floor; `charge=0` / `discharge=0` windows; `grid ≤ cap`.
* Primary: **LP via `scipy.optimize.linprog` (HiGHS)** — exact optimum for `min Σ grid×tariff` with battery/energy-balance/neutrality constraints. Simultaneous charge/discharge is canonicalised (never beneficial).
* Fallback: **pure-Python DP** (1 kWh levels anchored at `initial_energy`, cost = `grid×tariff` with maximal solar use, respecting all windows/caps). No extra dependency, guarantees a valid schedule if LP is missing/infeasible.
* Last resort: base-valid schedule ignoring directives (keeps HTTP 200 instead of 500).
* Totals (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`) are recomputed from `hourly_plan`.

## 5. API contract

* `GET /health` → `{"status":"ok"}`
* `POST /optimize-energy` → request/response exactly per Problem Statement. Malformed JSON/structural errors → `400`; semantic errors (negative values, min>capacity) → `422`; internal → `500` (no secrets/stack traces).

## 6. Public-sample test

```bash
python test_samples.py
# expected: 10/10 replay-valid, cost ratio 1.0000 vs reference
```

`test_samples.py` replays every `hourly_plan` against interpreted directives + battery/energy/neutrality rules and compares interpretation semantics (hours + numeric values, tolerance 0.02).

## 7. Docker fallback image

```bash
docker build -t gridwise-llm:latest .
docker run -p 8000:8000 -e GEMINI_API_KEY=... gridwise-llm:latest
curl http://localhost:8000/health
```

Image exposes `8000`, binds `0.0.0.0`, contains no secrets. Publish with an exact tag/digest (Docker Hub/GHCR) for submission.

## 8. Deployment (judge must reach it 7–11 PM)

Any reachable platform works (Render / Railway / Fly.io / VPS). Example (Render): new Web Service → from repo → build `pip install -r requirements.txt` → start `uvicorn app.main:app --host 0.0.0.0 --port $PORT` → set `GEMINI_API_KEY` env → verify `/health` + one sample from outside your network. Keep quota/rate limits in mind; the rule-parser fallback keeps you alive if the provider throttles.

## 9. Dependencies

`fastapi`, `uvicorn[standard]`, `numpy`, `scipy` (+ `httpx` for `TestClient`). LLM calls use stdlib only.

## 10. Known limitations

* DP fallback discretises battery to 1 kWh (cost error < ~1× max tariff vs LP optimum); LP is used whenever `scipy` is present.
* Bare time ranges without AM/PM (e.g. “one until three”) assume PM in solar/daytime context — correct for the challenge’s whole-hour convention but could misfire on genuinely ambiguous night windows; the LLM path resolves these when a key is configured.
* No grid export, 100% battery efficiency, and end-of-day neutrality are hard-coded per spec.
