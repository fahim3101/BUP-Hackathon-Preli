"""Paraphrase robustness eval (offline rule path + optional live LLM path).

Usage:
  python eval_paraphrases.py            # rule parser only, no key needed
  python eval_paraphrases.py --llm      # full interpret_notes (needs API key)
Target: 32/32 rule-only (safety net), LLM path must also be 32/32.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

USE_LLM = "--llm" in sys.argv
if not USE_LLM:
    os.environ["LLM_DISABLED"] = "1"

from app.interpreter import interpret_notes  # noqa: E402

P = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "paraphrases.json")))
CAP = P["capacity_kwh"]

fails = 0
for n in P["notes"]:
    e = interpret_notes([n["note"]], CAP)[0]
    a = e["structured_adjustment"] or {}
    good = (e["directive_type"] == n["type"])
    if good and n["type"] != "no_op":
        good = sorted(a.get("hours", [])) == sorted(n.get("hours", []))
        for k in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if k in n:
                good = good and abs(float(a.get(k, 9e9)) - float(n[k])) < 0.03
    elif n["type"] == "no_op":
        good = (e["directive_type"] == "no_op" and e["applies"] is False)
    if not good:
        fails += 1
        print("MISS", n["type"], "->", e["directive_type"], a.get("hours"), "|", n["note"][:70])

total = len(P["notes"])
print(f"{'LLM' if USE_LLM else 'RULE'}: {total - fails}/{total}")
sys.exit(1 if fails else 0)
