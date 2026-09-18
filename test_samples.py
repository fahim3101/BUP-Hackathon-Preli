"""Local validator: POSTs all 10 public samples, checks interpretation + replay."""
import json, math, os, sys

sys.path.insert(0, os.path.dirname(__file__))
from fastapi.testclient import TestClient
from app.main import app

TOL = 0.05  # slightly relaxed for DP-vs-LP cost comparison display

def replay(case_input, resp):
    hours = {h["hour"]: h for h in case_input["hours"]}
    bat = case_input["battery"]
    cap = bat["capacity_kwh"]; init = bat["initial_energy_kwh"]; base_min = bat["minimum_energy_kwh"]
    mc = bat["max_charge_kwh_per_hour"]; md = bat["max_discharge_kwh_per_hour"]
    eff = {h: hours[h]["solar_kwh"] for h in hours}
    active_min = {h: base_min for h in range(24)}
    no_ch, no_dis, caps = set(), set(), {}
    for e in resp["directive_interpretation"]:
        if not e["applies"]:
            continue
        a = e["structured_adjustment"]; dt = e["directive_type"]
        if dt == "solar_reduction":
            for h in a["hours"]:
                eff[h] *= a["factor"]
        elif dt == "minimum_battery_reserve":
            for h in a["hours"]:
                active_min[h] = max(active_min[h], a["minimum_energy_kwh"])
        elif dt == "no_charge_window":
            no_ch.update(a["hours"])
        elif dt == "no_discharge_window":
            no_dis.update(a["hours"])
        elif dt == "max_grid_window":
            for h in a["hours"]:
                caps[h] = min(caps.get(h, 1e18), a["max_grid_kwh"])
    plan = {p["hour"]: p for p in resp["hourly_plan"]}
    assert len(plan) == 24, "hourly_plan must have 24 entries"
    e_prev = init
    tg = tc = 0.0; peak = 0.0
    for h in range(24):
        p = plan[h]; dem = hours[h]["demand_kwh"]; tar = hours[h]["tariff_bdt_per_kwh"]
        g, s, act, kw, e_after = p["grid_kwh"], p["solar_used_kwh"], p["battery_action"], p["battery_kwh"], p["battery_energy_after_kwh"]
        assert g >= -0.01 and s >= -0.01 and kw >= -0.01, f"h{h} negative values"
        assert s <= eff[h] + 0.01, f"h{h} solar overuse {s} > {eff[h]}"
        if act == "charge":
            assert h not in no_ch, f"h{h} charge forbidden"
            assert kw <= mc + 0.01
            assert abs(e_after - (e_prev + kw)) < 0.02, f"h{h} battery transition"
        elif act == "discharge":
            assert h not in no_dis, f"h{h} discharge forbidden"
            assert kw <= md + 0.01
            assert abs(e_after - (e_prev - kw)) < 0.02, f"h{h} battery transition"
        else:
            assert kw == 0 or abs(kw) < 0.01
            assert abs(e_after - e_prev) < 0.02
        assert active_min[h] - 0.01 <= e_after <= cap + 0.01, f"h{h} battery bounds {e_after} vs min {active_min[h]}"
        c = kw if act == "charge" else 0.0
        d = kw if act == "discharge" else 0.0
        assert abs(g + s + d - (dem + c)) < 0.02, f"h{h} balance: {g}+{s}+{d} vs {dem}+{c}"
        if h in caps:
            assert g <= caps[h] + 0.01, f"h{h} grid cap"
        tg += g; tc += g * tar; peak = max(peak, g)
        e_prev = e_after
    assert abs(e_prev - init) < 0.02, f"neutrality {e_prev} vs {init}"
    assert abs(tg - resp["total_grid_kwh"]) < 0.05, f"total_grid {tg} vs {resp['total_grid_kwh']}"
    assert abs(tc - resp["total_cost_bdt"]) < 0.5, f"total_cost {tc} vs {resp['total_cost_bdt']}"
    assert abs(peak - resp["peak_grid_kwh"]) < 0.05
    return tg, tc

def same_interp(a, b):
    if a["directive_type"] != b["directive_type"] or a["applies"] != b["applies"]:
        return False
    sa, sb = a["structured_adjustment"], b["structured_adjustment"]
    if sa is None or sb is None:
        return sa is None and sb is None
    if sa.get("hours") != sb.get("hours"):
        return False
    for k in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
        if k in sa or k in sb:
            if abs(float(sa.get(k, 0)) - float(sb.get(k, 0))) > 0.02:
                return False
    return True

def main():
    os.environ["LLM_DISABLED"] = "1"  # offline deterministic check
    data = json.load(open("BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"))
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200 and r.json().get("status") == "ok", "health failed"
    print("health OK")
    fails = 0
    for case in data["cases"]:
        cid = case["id"]
        r = client.post("/optimize-energy", json=case["input"])
        if r.status_code != 200:
            print(f"{cid}: HTTP {r.status_code} {r.text[:200]}"); fails += 1; continue
        resp = r.json()
        exp = case["expected_output"]["directive_interpretation"]
        got = resp["directive_interpretation"]
        imatch = len(exp) == len(got) and all(same_interp(g, e) for g, e in zip(got, exp))
        try:
            tg, tc = replay(case["input"], resp)
            exp_cost = case["expected_output"]["total_cost_bdt"]
            ratio = exp_cost / tc if tc else 1.0
            flag = "OK " if imatch else "INTERP-DIFF"
            print(f"{cid}: {flag} replay-valid cost={tc:.1f} ref={exp_cost} ratio={ratio:.4f}")
            if not imatch:
                print(f"  expected={exp}")
                print(f"  got     ={got}")
                fails += 1
        except AssertionError as ex:
            print(f"{cid}: REPLAY-FAIL {ex}"); fails += 1
    print("FAILURES:", fails)
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
