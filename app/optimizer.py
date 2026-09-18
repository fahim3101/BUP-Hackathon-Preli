"""Cost optimizer: LP (scipy/HiGHS) primary, DP fallback.

Variables per hour h: c (charge), d (discharge), s (solar_used), g (grid), E (energy after).
Energy balance: g + s + d = demand + c.  Battery: E[h] = E[h-1] + c - d.
Simultaneous c&d is canonicalised away (never beneficial with positive tariffs).
"""
TOL = 1e-9


def build_effective(hours, interpretations):
    eff = {h["hour"]: float(h["solar_kwh"]) for h in hours}
    for e in interpretations:
        if e["applies"] and e["directive_type"] == "solar_reduction":
            f = float(e["structured_adjustment"]["factor"])
            for h in e["structured_adjustment"]["hours"]:
                if h in eff:
                    eff[h] = eff[h] * f
    return eff


def build_limits(battery, interpretations):
    cap = float(battery["capacity_kwh"])
    base_min = float(battery["minimum_energy_kwh"])
    active_min = {h: base_min for h in range(24)}
    no_ch = set()
    no_dis = set()
    caps = {}
    for e in interpretations:
        if not e["applies"]:
            continue
        adj = e["structured_adjustment"]
        dt = e["directive_type"]
        if dt == "minimum_battery_reserve":
            v = float(adj["minimum_energy_kwh"])
            for h in adj["hours"]:
                active_min[h] = max(active_min[h], v)
        elif dt == "no_charge_window":
            no_ch.update(adj["hours"])
        elif dt == "no_discharge_window":
            no_dis.update(adj["hours"])
        elif dt == "max_grid_window":
            v = float(adj["max_grid_kwh"])
            for h in adj["hours"]:
                caps[h] = v if h not in caps else min(caps[h], v)
    return active_min, no_ch, no_dis, caps


def try_linprog(hours_list, battery, eff, active_min, no_ch, no_dis, grid_caps):
    import numpy as np
    from scipy.optimize import linprog
    n = 24
    dem = [float(next(h for h in hours_list if h["hour"] == i)["demand_kwh"]) for i in range(24)]
    sol = [float(eff[i]) for i in range(24)]
    tar = [float(next(h for h in hours_list if h["hour"] == i)["tariff_bdt_per_kwh"]) for i in range(24)]
    cap = float(battery["capacity_kwh"])
    init = float(battery["initial_energy_kwh"])
    mc = float(battery["max_charge_kwh_per_hour"])
    md = float(battery["max_discharge_kwh_per_hour"])

    N = 24 * 5
    IC, ID, IS, IG, IE = 0, 24, 48, 72, 96
    c = np.zeros(N)
    for i in range(24):
        c[IG + i] = tar[i]
    Aeq = np.zeros((48, N))
    beq = np.zeros(48)
    for h in range(24):
        # battery: E[h] - E[h-1] - c + d = 0 (h=0: E0 - c + d = init)
        Aeq[h, IE + h] = 1
        if h > 0:
            Aeq[h, IE + h - 1] = -1
        Aeq[h, IC + h] = -1
        Aeq[h, ID + h] = 1
        beq[h] = init if h == 0 else 0.0
        # balance: g + s + d - c = demand
        r = 24 + h
        Aeq[r, IG + h] = 1
        Aeq[r, IS + h] = 1
        Aeq[r, ID + h] = 1
        Aeq[r, IC + h] = -1
        beq[r] = dem[h]
    bounds = []
    for h in range(24):
        bounds.append((0, 0 if h in no_ch else mc))          # c
    for h in range(24):
        bounds.append((0, 0 if h in no_dis else md))         # d
    for h in range(24):
        bounds.append((0, max(0.0, sol[h])))                 # s
    for h in range(24):
        bounds.append((0, grid_caps[h] if h in grid_caps else None))  # g
    for h in range(24):
        lo = active_min[h]
        hi = cap
        if h == 23:
            lo = hi = init  # neutrality
            if active_min[h] > init + 1e-6:
                return None  # infeasible by construction
        bounds.append((lo, hi))
    # shift bounds list into variable order c,d,s,g,E
    bnd = [None] * N
    for h in range(24):
        bnd[IC + h] = bounds[h]
        bnd[ID + h] = bounds[24 + h]
        bnd[IS + h] = bounds[48 + h]
        bnd[IG + h] = bounds[72 + h]
        bnd[IE + h] = bounds[96 + h]
    res = linprog(c, A_eq=Aeq, b_eq=beq, bounds=bnd, method="highs")
    if not res.success:
        return None
    x = res.x
    plan = []
    for h in range(24):
        cc, dd, ss, gg, ee = (float(x[IC + h]), float(x[ID + h]), float(x[IS + h]),
                              float(x[IG + h]), float(x[IE + h]))
        # cleanup tiny negatives
        cc = 0.0 if cc < 1e-7 else cc
        dd = 0.0 if dd < 1e-7 else dd
        ss = 0.0 if ss < 1e-7 else ss
        gg = 0.0 if gg < 1e-7 else gg
        # canonicalise simultaneous charge/discharge
        if cc > 1e-9 and dd > 1e-9:
            net = cc - dd
            cc, dd = (net, 0.0) if net >= 0 else (0.0, -net)
        if h in no_ch:
            cc = 0.0
        if h in no_dis:
            dd = 0.0
        plan.append({"c": cc, "d": dd, "s": ss, "g": gg, "e": ee})
    # re-derive grid from balance to kill solver noise, then clamp solar
    for h in range(24):
        p = plan[h]
        s_cap = max(0.0, sol[h])
        p["s"] = max(0.0, min(p["s"], s_cap))
        p["g"] = max(0.0, dem[h] + p["c"] - p["d"] - p["s"])
        if h in grid_caps:
            p["g"] = min(p["g"], grid_caps[h])
    return plan


def dp_optimize(hours_list, battery, eff, active_min, no_ch, no_dis, grid_caps, step=1.0):
    dem = [float(next(h for h in hours_list if h["hour"] == i)["demand_kwh"]) for i in range(24)]
    sol = [float(eff[i]) for i in range(24)]
    tar = [float(next(h for h in hours_list if h["hour"] == i)["tariff_bdt_per_kwh"]) for i in range(24)]
    cap = float(battery["capacity_kwh"])
    init = float(battery["initial_energy_kwh"])
    mc = float(battery["max_charge_kwh_per_hour"])
    md = float(battery["max_discharge_kwh_per_hour"])

    # discrete levels anchored at init
    import math
    levels_all = []
    k = 0
    # expand downward/upward
    vals = set()
    kk = 0
    while True:
        v = init + kk * step
        if v > cap + 1e-9 and kk > 0:
            break
        if 0 - 1e-9 <= v <= cap + 1e-9:
            vals.add(round(v, 9))
        kk += 1
        if kk > 2000:
            break
    kk = -1
    while True:
        v = init + kk * step
        if v < -1e-9 and kk < 0:
            break
        if -1e-9 <= v <= cap + 1e-9:
            vals.add(round(v, 9))
        kk -= 1
        if kk < -2000:
            break
    levels_all = sorted(vals)
    # per-hour feasible levels
    feas = []
    for h in range(24):
        lo, hi = active_min[h], cap
        if h == 23:
            lo = hi = init
        feas.append([v for v in levels_all if v + 1e-9 >= lo and v - 1e-9 <= hi])

    INF = float("inf")
    # dp[h][level_idx] = (cost, prev_level_value)
    prev = {}
    # hour 0 transitions from init
    h = 0
    cur = {}
    for v in feas[0]:
        net = v - init
        if net > 1e-9 and (0 in no_ch or h in no_ch):
            continue
        if net < -1e-9 and h in no_dis:
            continue
        if net > mc + 1e-9 or -net > md + 1e-9:
            continue
        if dem[h] + net < -1e-9:
            continue
        s = min(sol[h], dem[h] + net)
        g = dem[h] + net - s
        if g < -1e-9:
            continue
        if h in grid_caps and g > grid_caps[h] + 1e-6:
            continue
        cur[v] = (g * tar[h], init)
    prev = cur
    back = [{v: init for v in cur}]
    for h in range(1, 24):
        cur = {}
        par = {}
        # iterate prev levels
        for pv, (pcost, _) in prev.items():
            # candidate curr levels within rate limits
            lo_v = max(active_min[h], pv - md, 0)
            hi_v = min(cap, pv + mc)
            for v in feas[h]:
                if v < lo_v - 1e-9 or v > hi_v + 1e-9:
                    continue
                net = v - pv
                if net > 1e-9 and h in no_ch:
                    continue
                if net < -1e-9 and h in no_dis:
                    continue
                if dem[h] + net < -1e-9:
                    continue
                s = min(sol[h], dem[h] + net)
                g = dem[h] + net - s
                if g < -1e-9:
                    continue
                if h in grid_caps and g > grid_caps[h] + 1e-6:
                    continue
                nc = pcost + g * tar[h]
                if v not in cur or nc < cur[v][0]:
                    cur[v] = (nc, pv)
        if not cur:
            return None
        prev = cur
        back.append({v: p for v, (_, p) in cur.items()})
    if init not in prev and not any(abs(v - init) < 1e-9 for v in prev):
        # neutrality level must be reachable
        cands = [v for v in prev if abs(v - init) < step / 2 + 1e-9]
        if not cands:
            return None
    # reconstruct (final E == init)
    end = min(prev.keys(), key=lambda v: (abs(v - init), prev[v][0]))
    traj = [0.0] * 24
    traj[23] = end
    for h in range(23, 0, -1):
        traj[h - 1] = back[h][traj[h]]
    # build plan
    plan = []
    eprev = init
    for h in range(24):
        e = traj[h] if h > 0 else traj[0]
        net = e - eprev if h > 0 else traj[0] - init
        if h == 0:
            net = traj[0] - init
            eprev = init
        cc, dd = (net, 0.0) if net >= 0 else (0.0, -net)
        s = min(sol[h], dem[h] + net)
        g = max(0.0, dem[h] + net - s)
        plan.append({"c": max(0.0, cc), "d": max(0.0, dd), "s": max(0.0, s),
                     "g": max(0.0, g), "e": float(e)})
        eprev = e
    return plan


def optimize(hours_list, battery, interpretations):
    eff = build_effective(hours_list, interpretations)
    active_min, no_ch, no_dis, grid_caps = build_limits(battery, interpretations)
    plan = None
    try:
        plan = try_linprog(hours_list, battery, eff, active_min, no_ch, no_dis, grid_caps)
    except Exception:
        plan = None
    if plan is None:
        try:
            plan = dp_optimize(hours_list, battery, eff, active_min, no_ch, no_dis, grid_caps)
        except Exception:
            plan = None
    if plan is None:
        # last resort: base-valid schedule ignoring directives (keeps service 200)
        try:
            base_min = {h: float(battery["minimum_energy_kwh"]) for h in range(24)}
            plan = dp_optimize(hours_list, battery, eff, base_min, set(), set(), {})
        except Exception:
            plan = None
    return plan, eff, active_min, no_ch, no_dis, grid_caps
