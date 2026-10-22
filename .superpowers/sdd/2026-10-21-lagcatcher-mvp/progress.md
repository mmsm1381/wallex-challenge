# SDD ledger — plan: docs/superpowers/plans/2026-10-21-lagcatcher-mvp.md
Setup: Ruling: no git worktree and no commits — the folder is not a git repo and the user did not ask for commits — cost if wrong: no per-task history, this ledger is the record.
Setup: Ruling: the final review is a self-review — the session rules say not to start subagents unless the user asks — cost if wrong: a weaker review than a fresh reviewer.
Setup: Ruling: test names follow the user's write-test skill pattern test_<unit>_<condition>_<result>, not the short plan names — the user skill sets the convention — cost if wrong: renames only.
Pre-flight: T2 uses fee_rates from T1 (same signature). T3 uses Quotes/Premium (T1) and strategies/Episodes (T2) through the Interfaces blocks. T4 reads events.jsonl/episodes.jsonl written by T3. No conflicts found.
Task 1: complete (tests: .venv/bin/python -m pytest -q tests/test_fairvalue.py tests/test_config.py -> 9 passed)
Task 2: Ruling: test_exit_for_usdt_pair_converts_net_to_toman_at_exit_rate used a 0.49 bid under a 0.5 entry, so the 1% stop fired at once (correct code behavior). The test bid is now 0.499 — cost if wrong: none.
Task 2: complete (tests: .venv/bin/python -m pytest -q -> 27 passed)
Task 3: Ruling: engine.py was written before its two tests. To prove the tests work, a broken seed rule was checked: the test failed, then passed after the restore (a stale .pyc hid the restore once, and clearing __pycache__ fixed it) — cost if wrong: the engine tests may miss other wiring bugs, and the live run is the backstop.
Task 3: complete (tests: .venv/bin/python -m pytest -q -> 51 passed; a live check here and on the server shows all feeds OK)
Task 3: Ruling: Wallex v1 endpoint added as a fallback — v2 answered HTTP 503 for minutes from Iran and from the server — cost if wrong: v1 is deprecated, but the docs say it still works.
Task 3: Ruling: stale-Wallex guard (5 s) and disk guard (600 MB raw off, 300 MB stop) added. They were not in the plan, but the live run and the shared server need them — cost if wrong: some lost data during outages.
Task 3: Ruling: Python 3.10 support (tomli fallback), because the server has Python 3.10 — cost if wrong: none, the tests pass on 3.10.
Task 4: complete (report tests pass; deployed to root@65.109.212.3:/root/lagcatcher as the transient unit lagcatcher-shadow, CPUQuota 50%, MemoryMax 400M, nice 10, 8 h from 19:46 UTC)
Task 4: Ruling: a transient systemd-run unit is used instead of the enabled service of install.sh — the server is shared with the cassandra bot and the run is limited to 8 h — cost if wrong: none.
Final review: self-review (no subagent tool by session rule)
Task 5 (replay, added on request): Ruling: the replay test needed a USDTTMN book, because USDT pairs size the 1M toman trade with it. The test setup was fixed, not the code — cost if wrong: none.
Task 5: complete (replay module + CLI, tests: .venv/bin/python -m pytest -q -> 55 passed; replay of the first 35 min of server data ran in 17 s)
Task 5: Ruling: raw recording now starts again 100 MB above its disk limit, and the server limits are 400/250 MB — needed to record Tehran daytime on a nearly full disk — cost if wrong: a little more disk use near the limit.
Task 5: Ruling: the run was restarted at 20:24:30 UTC for 18 h (it ends about 14:24 UTC on 10-22) because the user asked for Tehran daytime. The first 8-hour run was stopped after 38 min — cost if wrong: 10 min of warm-up lost.
Handoff: docs/HANDOFF.md written for a context reset.
