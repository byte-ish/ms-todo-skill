# Contributing

Thanks for taking a look. Issues and pull requests are both welcome.

## Getting set up

```bash
git clone https://github.com/byte-ish/ms-todo-skill.git
cd ms-todo-skill
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the checks before you push:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

The test suite is fast (under a second) and touches neither the network nor your
real config directory, so there is no excuse for not running it.

## The one hard rule

**No runtime dependencies.** The package imports nothing outside the standard
library, and CI has a job that proves it by running the CLI in a bare
interpreter with no `pip install`. This is what lets people drop the repo onto a
locked-down box and have it work. Test-only dependencies are fine.

If you genuinely need something the stdlib can't do, open an issue first and make
the case — the answer isn't automatically no, but the bar is high.

## How the code is arranged

| Module | Responsibility |
| --- | --- |
| `scripts/todo.py` | entry point; sets `sys.path` and calls into the package |
| `mstodo/http.py` | transport: retries, backoff, `Retry-After`. Knows nothing about Graph |
| `mstodo/auth.py` | device code flow, token cache, refresh |
| `mstodo/graph.py` | Graph client: auth injection, error translation, paging, delta |
| `mstodo/service.py` | To Do domain operations, reference resolution, client-side filtering |
| `mstodo/models.py` | payload construction, enum normalisation, recurrence specs |
| `mstodo/dates.py` | human date parsing and `dateTimeTimeZone` conversion |
| `mstodo/format.py` | terminal rendering. JSON output never passes through here |
| `mstodo/cli.py` | argparse tree and command handlers |
| `mstodo/errors.py` | typed errors and their exit codes |

Layers point downward only. `service.py` may import `graph.py`; `graph.py` must
never import `service.py`.

## Conventions worth keeping

- **Every mutation honours `--dry-run`.** If you add a write path, make sure it
  is routed through `GraphClient.request` so the flag works for free.
- **Destructive commands require confirmation.** `ctx.confirm()` refuses to
  proceed non-interactively without `--yes`.
- **Exit codes are a contract.** They are documented in the README, `SKILL.md`
  and `references/api-reference.md`. Renumbering one is a breaking change.
- **`--json` prints unmodified Graph objects.** Do not reshape them; scripts
  depend on the raw structure. The `_listName` / `_listId` additions on `ls` are
  the deliberate exception and are documented.
- **Errors carry a `hint`.** If a user can hit an error, tell them what to do
  about it in the same breath.
- **Never log or print a token.** Not even at `-vv`.

## Tests

Add tests with behaviour, not after it. The fixtures in `tests/conftest.py` give
you:

- `transport` — a fake `urlopen` that records requests and replies from a queue
  of routes. Routes match on HTTP method and a **path suffix**, deliberately not
  a bare substring, so `/me/todo/lists` cannot silently intercept
  `/me/todo/lists/{id}/tasks`.
- `signed_in` — writes a valid-looking token cache so Graph calls skip login.
- `isolated_config` — points `MSTODO_CONFIG_DIR` at a temp directory. Autouse,
  so no test can touch your real credentials.

Prefer asserting on the request that was sent over asserting on printed output;
the wire format is the thing that has to stay right.

## Verifying against the real API

Unit tests cover the logic, but before releasing anything that touches request
shapes, run it against a real mailbox with a scratch list:

```bash
todo.py lists new "ms-todo-skill scratch"
todo.py add "smoke test" -l "ms-todo-skill scratch" --due tomorrow --recur weekly
todo.py get "smoke test" -l "ms-todo-skill scratch"
todo.py lists rm "ms-todo-skill scratch" --yes
```

Never commit a real client id, tenant id, task id or token in a test fixture or
an issue.

## Pull requests

Keep them focused, explain the behaviour change in the description, and update
`references/api-reference.md` when you add or change a flag. Add a `CHANGELOG.md`
entry under *Unreleased*.

Commit messages: imperative mood, one line summary, body if the reasoning isn't
obvious from the diff.

## Reporting bugs

Include the command you ran, the full error, and the output of
`todo.py --version`. Add `-vv` for debug logging — it never prints tokens, but do
skim it before pasting.

Security issues go to [SECURITY.md](SECURITY.md), not the public tracker.
