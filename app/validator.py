"""Judge-style replay validator: every response is self-checked before it
leaves the service, so an invalid schedule never reaches the judge."""
TOL = 0.011


def replay(body, interpretations, hourly_plan):
    """Return list of error strings (empty = valid)."""
    errs = []
    hours = {h["hour"]: h for h in body["hours"]}
    bat = body["battery"]
    cap = float(bat["capacity_kwh"])
    init = float(bat["initial_energy_kwh"])
    base_min = float(bat["minimum_energy_kwh"])
    mc = float(bat["max_charge_kwh_per_hour"])
    md = float(bat["max_discharge_kwh_per_hour"])

    eff = {h: float(hours[h]["solar_kwh"]) for h in hours}
    active_min = {h: base_min for h in range(24)}
    no_ch, no_dis, caps = set(), set(), {}
    for e in interpretations:
        if not e.get("applies"):
            continue
        a = e["structured_adjustment"] or {}
        dt = e["directive_type"]
        try:
            if dt == "solar_reduction":
                for h in a["hours"]:
                    eff[h] *= float(a["factor"])
            elif dt == "minimum_battery_reserve":
                for h in a["hours"]:
                    active_min[h] = max(active_min[h], float(a["minimum_energy_kwh"]))
            elif dt == "no_charge_window":
                no_ch.update(a["hours"])
            elif dt == "no_discharge_window":
                no_dis.update(a["hours"])
            elif dt == "max_grid_window":
                for h in a["hours"]:
                    caps[h] = min(caps.get(h, 1e18), float(a["max_grid_kwh"]))
        except Exception as ex:
            errs.append(f"directive replay: {ex}")
    if len(hourly_plan) != 24:
        errs.append("hourly_plan must have 24 entries")
        return errs
    plan = {p["hour"]: p for p in hourly_plan}
    if set(plan) != set(range(24)):
        errs.append("hourly_plan must cover 0..23")

    e_prev = init
    tg = tc = 0.0
    peak = 0.0
    for h in range(24):
        p = plan.get(h, {})
        try:
            g = float(p["grid_kwh"])
            s = float(p["solar_used_kwh"])
            act = p["battery_action"]
            kw = float(p["battery_kwh"])
            e_after = float(p["battery_energy_after_kwh"])
        except Exception:
            errs.append(f"h{h}: bad types")
            continue
        dem = float(hours[h]["demand_kwh"])
        tar = float(hours[h]["tariff_bdt_per_kwh"])
        if g < -TOL or s < -TOL or kw < -TOL:
            errs.append(f"h{h}: negative values")
        if s > eff[h] + TOL:
            errs.append(f"h{h}: solar overuse")
        if act == "charge":
            if h in no_ch:
                errs.append(f"h{h}: charge forbidden")
            if kw > mc + TOL:
                errs.append(f"h{h}: charge rate")
            if abs(e_after - (e_prev + kw)) > 0.02:
                errs.append(f"h{h}: battery transition")
            c, d = kw, 0.0
        elif act == "discharge":
            if h in no_dis:
                errs.append(f"h{h}: discharge forbidden")
            if kw > md + TOL:
                errs.append(f"h{h}: discharge rate")
            if abs(e_after - (e_prev - kw)) > 0.02:
                errs.append(f"h{h}: battery transition")
            c, d = 0.0, kw
        elif act == "idle":
            if abs(kw) > TOL:
                errs.append(f"h{h}: idle must have 0 kwh")
            if abs(e_after - e_prev) > 0.02:
                errs.append(f"h{h}: battery transition")
            c, d = 0.0, 0.0
        else:
            errs.append(f"h{h}: bad action")
            c, d = 0.0, 0.0
        if not (active_min[h] - TOL <= e_after <= cap + TOL):
            errs.append(f"h{h}: battery bounds")
        if abs(g + s + d - (dem + c)) > 0.02:
            errs.append(f"h{h}: energy balance")
        if h in caps and g > caps[h] + TOL:
            errs.append(f"h{h}: grid cap")
        tg += g
        tc += g * tar
        peak = max(peak, g)
        e_prev = e_after
    if abs(e_prev - init) > 0.02:
        errs.append("end-of-day neutrality")
    return errs
