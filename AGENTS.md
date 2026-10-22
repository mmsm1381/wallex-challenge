# Instructions for agents

Read this file before you change anything in this repository. It applies to every agent (Claude Code, Codex and others) and to every human contributor.

## 1. Where to start

1. Read `README.md` for the project and its status.
2. Read `docs/IMPLEMENTATION_PLAN.md`. Take the first open task (`[ ]`).
3. Read the parts of `docs/DESIGN.md` that the task names.
4. Read `docs/RESEARCH.md` when the task touches a number (depth, filter, limit, fee).
5. `docs/HANDOFF.md` is the history log of the project. Read it when you need the reason for an earlier decision.

## 2. Hard rules

* **Shared server.** The shadow-test server also runs the trading bot "cassandra" of a team member. Do not stop, restart, change or clean up anything of cassandra. Before and after each action on the server, check it with `systemctl show cassandra -p ActiveState -p MainPID -p NRestarts`. The values must not change.
* **Disk cleanup on the server.** Ask the user before you delete any file on the server.
* **No secrets in git.** API keys come from environment variables (`WALLEX_API_KEY`, `ANTHROPIC_API_KEY`) or from a `.env` file that git ignores. Never print a key in a log.
* **No live orders without a person.** Live orders (milestone 2.4 and later) need a team member who is present and agrees.
* **No commits or pushes without a request.** Commit only when the user asks. Push only when the user asks.
* **Numbers come from data.** Do not change a depth, a filter threshold or a limit without a backtest. Record each new result in `docs/RESEARCH.md` with its script and data.

## 3. How to work

* Environment: `uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'`
* Tests: `.venv/bin/python -m pytest -q`. All tests must pass before a commit.
* Write the test first. Use the style of `tests/`: a clear test name, a docstring with the scenario, and `# given`, `# when`, `# then` sections.
* Follow the Google Python style. Match the naming, comments and docstrings of the code around your change.
* Keep each module small and with one job, as in `docs/DESIGN.md` section 4.
* Update `docs/IMPLEMENTATION_PLAN.md` (mark the task `[x]`) and the docs in the same change as the code.

## 4. Writing style for docs, comments and commit messages

Use simple technical English (ASD-STE100 style):

* Short sentences: 20 words or fewer for instructions, 25 or fewer for descriptions.
* One instruction per sentence. Active voice. No semicolons.
* Use one word for one thing in a document. Do not rotate synonyms.
* Use lists for 3 or more steps or conditions.
* Keep hedges ("may", "about") when the data is not certain.

## 5. Environment facts

* The developer machine is in Iran. Its `ALL_PROXY` setting points to a dead proxy. Bypass it: `curl --noproxy '*'`, `proxy=None` in `websockets`, and `urllib.request.ProxyHandler({})`.
* From Iran, api.binance.com and OKX block access. Wallex, KuCoin, Gate and data.binance.vision work.
* The shadow-test server is outside Iran. Binance (port 443, `data-stream.binance.vision`), OKX, Bybit, KuCoin, Gate and Wallex work there.
* `pkill -f <pattern>` and `kill $(ps aux | grep <pattern>)` also match the shell that runs them when the pattern is in the command line. Use the exact PID.
* Wallex USDTTMN closes every night from 23:30 to 11:30 Tehran time (20:00 to 08:00 UTC).
