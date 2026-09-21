# PLAYBOOK — porer hackathon-e notun chat-ke ja bolba

> AI memory thake na. Tai sob shikkha ei file-e. Notun event-e notun chat khule
> sudhu bolo: "Repo-r PLAYBOOK.md poro, CHECKLIST-er protita item verify kore
> tobe code-e hat diba." Eitukui enough.

## 1. Context (ei repo)

GridWise LLM energy optimizer — BUP CSE Fest 2026 preliminary. `POST /optimize-energy`
LLM diye operator note pore, guardrail diye check kore, LP (scipy/HiGHS) diye
24h schedule banay. Public 10/10 pass chilo, tobu select hoyni.

## 2. Postmortem — keno harlam (mapa number soho)

1. Paraphrase gap: amader rule fallback hidden-style 31 note-e **20/31**,
   selected team **31/31**. Miss list: `halve`, `cutting...by X%` (factor ulta),
   `produce nothing` (=0), single-hour window, `storage` (=battery),
   `until midnight` end, overnight wrap (`10 PM to 1 AM`), `cushion` (=reserve),
   `supply/deliver power` (=discharge). Public-e 10/10 manei ready NA.
2. Single LLM provider (Gemini) chilo tokhon. Judge burst-e 429/503 khele
   fallback-e portam, jekhane accuracy chilo 65% (ekhon §5-er fix-er por 31/31).
   Oder chilo 5 model x 2 provider + cooldown — porer bar 2 provider mandatory.
3. Prompt contract durbol: LLM-er kache final hours+factor chawa hoise.
   Thik design: LLM dibe `windows [start,end)` + `value+unit`
   (remaining vs reduction), guner hisab CODE korbe.
4. In-service self-check chilo na (sudhu offline script). Return-er age
   replay-validator chalano mandatory.
5. Free Render (sleep/cold-start risk) vs always-on host. Judging night-e
   paid tier + warm-ping du toi rakha uchit chilo.

## 3. CHECKLIST — submit-er age sob tick na hole submit na

- [ ] Public samples: interpretation 10/10 + cost exact (script diye, hit-or-miss na)
- [ ] Nijer lekha 30+ paraphrase set-e fallback 100% (overnight, midnight-end,
      single-hour, halve/nothing/cushion/supply/storage sob cover)
- [ ] Prompt: LLM sudhu windows+unit dey, arithmetic code-e; strict JSON schema
- [ ] 2 provider x 2-3 model (primary fast + strict-schema, backup onno company,
      optional choto 3rd). 4/5-ta lagbe na — 2-tar por benefit kome, code jotil
      hoy. Protita model-ke paraphrase set-e alada pass korate hobe; 429-e
      per-model cooldown + total time budget rakho. Model naam event day-te
      verify koro (naam 6 mase bodlay — amader 2.0-flash → 3.6-flash hoisilo).
- [ ] p95 latency mapo (<5s target); per-request LLM time log koro
- [ ] Service-e self-check replay (invalid response baire jabe na)
- [ ] PUBLIC URL-er biruddhe full 10 sample pass (localhost-e na)
- [ ] Docker image push + digest README-te + `docker pull/run` verify
- [ ] Cron warm-ping + judging night-e paid tier bibechona
- [ ] Repo public timing, video link (logout-e khule?), secret leak check
      (PowerShell: `git log -p | Select-String "API_KEY|api_key|AIza|AQ\."` khali thakte hobe)
- [ ] Judge test korlo kina Render log-e `POST` search diye confirm

## 4. Notun chat-ke dewar starter prompt (copy-paste)

"Ei repo-r PLAYBOOK.md §2 (postmortem), §3 (checklist) ar §5 (hardening) poro.
`git log --oneline -5` dekhe current state bojho. Kono code change-er age
checklist-er relevant item diye verify koro, change-er poreo abar verify koro
(`python test_samples.py` = 10/10, `python eval_paraphrases.py` = 32/32 thakte
hobe). Ekta file change kore ekta commit+push (message English-e, choto).
Secret (key/token) kokhono chat-e, code-e ba git-e diba na."

## 5. Post-event hardening (done, commits after `7fb8f0a`)

- `app/units.py` (new): LLM + rule parser emit windows+unit only; conversion
  centralized — LLM arithmetic mistakes impossible by construction.
- `app/rule_parser.py`: 11 miss fixed → own 32-set **32/32**, oder 31-set **31/31**.
- `app/validator.py` (new) + `app/main.py`: every response replay-checked
  before return; failing plan falls back to base-valid schedule (never 500).
- `tests/paraphrases.json` (32 own notes) + `eval_paraphrases.py`
  (`--llm` flag tests the live LLM path too).
- Gemini strict `response_json_schema` + 8s timeout + browser UA + 5xx retry.
- Verify anytime: `python test_samples.py` (10/10) + `python eval_paraphrases.py` (32/32).

## 6. Multi-chat niyom (eker odhik chat use korle)

- Single source of truth = git HEAD. Onno chat diye kaj korale age `git pull`,
  pore `git log --oneline -5` + `git status` diye milaw — divergent change
  thakle age merge, tarpor notun kaj.
- Dashboard/Hub-er kaj (Render env, Docker push, cron) git-e dekha jay na —
  ogulo baire theke verify koro: onno network theke `docker pull/run` + PUBLIC
  URL-e full 10 sample. Localhost pass manei submit-ready NA.
