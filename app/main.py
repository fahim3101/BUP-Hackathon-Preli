"""GridWise LLM-assisted energy optimization API."""
import json
import math
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .interpreter import interpret_notes
from .optimizer import optimize

app = FastAPI(title="GridWise LLM Energy Optimizer")


@app.get("/health")
def health():
    return {"status": "ok"}


def _is_finite_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_request(body):
    """Return (ok, error_msg, parsed). Structural -> 400, semantic -> 422."""
    if not isinstance(body, dict):
        return False, "body must be object", (400, None)
    for f in ("scenario_id", "operator_notes", "hours", "battery"):
        if f not in body:
            return False, f"missing field: {f}", (400, None)
    if not isinstance(body["scenario_id"], str) or not body["scenario_id"]:
        return False, "scenario_id must be non-empty string", (400, None)
    notes = body["operator_notes"]
    if not isinstance(notes, list) or not (1 <= len(notes) <= 3):
        return False, "operator_notes must have 1..3 items", (400, None)
    for n in notes:
        if not isinstance(n, str) or not n.strip():
            return False, "operator_notes items must be non-empty strings", (400, None)
    hours = body["hours"]
    if not isinstance(hours, list) or len(hours) != 24:
        return False, "hours must contain exactly 24 entries", (400, None)
    seen = set()
    for e in hours:
        if not isinstance(e, dict):
            return False, "hour entry must be object", (400, None)
        for f in ("hour", "demand_kwh", "solar_kwh", "tariff_bdt_per_kwh"):
            if f not in e:
                return False, f"hour missing {f}", (400, None)
        h = e["hour"]
        if not isinstance(h, int) or isinstance(h, bool) or not (0 <= h <= 23):
            return False, "hour must be int 0..23", (400, None)
        if h in seen:
            return False, "duplicate hour", (400, None)
        seen.add(h)
        for f in ("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh"):
            if not _is_finite_number(e[f]) or e[f] < 0:
                return False, f"{f} must be finite non-negative", (422, None)
    bat = body["battery"]
    if not isinstance(bat, dict):
        return False, "battery must be object", (400, None)
    for f in ("capacity_kwh", "initial_energy_kwh", "minimum_energy_kwh",
              "max_charge_kwh_per_hour", "max_discharge_kwh_per_hour"):
        if f not in bat:
            return False, f"battery missing {f}", (400, None)
        if not _is_finite_number(bat[f]) or bat[f] < 0:
            return False, f"battery.{f} must be finite non-negative", (422, None)
    if bat["minimum_energy_kwh"] > bat["capacity_kwh"]:
        return False, "minimum exceeds capacity", (422, None)
    if bat["initial_energy_kwh"] > bat["capacity_kwh"] or bat["initial_energy_kwh"] < bat["minimum_energy_kwh"]:
        return False, "initial energy outside bounds", (422, None)
    if set(seen) != set(range(24)):
        return False, "hours must cover 0..23", (400, None)
    return True, "", None


@app.post("/optimize-energy")
async def optimize_energy(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "malformed JSON"})
    ok, msg, code = validate_request(body)
    if not ok:
        status = code[0] if code else 400
        return JSONResponse(status_code=status, content={"error": msg})
    try:
        scenario_id = body["scenario_id"]
        notes = body["operator_notes"]
        hours = sorted(body["hours"], key=lambda x: x["hour"])
        battery = body["battery"]
        cap = float(battery["capacity_kwh"])

        interpretations = interpret_notes(notes, cap)
        plan_raw, eff, active_min, no_ch, no_dis, grid_caps = optimize(hours, battery, interpretations)
        if plan_raw is None:
            return JSONResponse(status_code=500, content={"error": "infeasible scenario"})
        dem = {h["hour"]: float(h["demand_kwh"]) for h in hours}
        tar = {h["hour"]: float(h["tariff_bdt_per_kwh"]) for h in hours}
        hourly_plan = []
        total_grid = 0.0
        total_cost = 0.0
        peak = 0.0
        for h in range(24):
            p = plan_raw[h]
            g = round(float(p["g"]), 6)
            s = round(float(p["s"]), 6)
            c, d = float(p["c"]), float(p["d"])
            e = round(float(p["e"]), 6)
            if c < 0.0005 and d < 0.0005:
                act, kw = "idle", 0.0
            elif c >= d:
                act, kw = "charge", round(c, 6)
            else:
                act, kw = "discharge", round(d, 6)
            hourly_plan.append({"hour": h, "grid_kwh": g, "solar_used_kwh": s,
                                "battery_action": act, "battery_kwh": kw,
                                "battery_energy_after_kwh": e})
            total_grid += g
            total_cost += g * tar[h]
            peak = max(peak, g)
        summary_bits = []
        for e in interpretations:
            if e["applies"]:
                adj = e["structured_adjustment"]
                summary_bits.append(f"{e['directive_type']} {adj.get('hours', [])}")
        strat = ("Applied " + "; ".join(summary_bits)) if summary_bits else "No applicable directives"
        plan_summary = (f"{strat}. Minimized grid cost with battery arbitrage "
                        f"while restoring initial energy. Cost BDT {total_cost:.2f}.")[:500]
        return {"scenario_id": scenario_id,
                "directive_interpretation": interpretations,
                "hourly_plan": hourly_plan,
                "total_grid_kwh": round(total_grid, 6),
                "total_cost_bdt": round(total_cost, 6),
                "peak_grid_kwh": round(peak, 6),
                "plan_summary": plan_summary}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": "internal error"})
