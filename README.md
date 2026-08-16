# ms-todo-skill

Manage **Microsoft To Do** from your terminal — and from Claude — over the
Microsoft Graph v1.0 API.

[![CI](https://github.com/byte-ish/ms-todo-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/byte-ish/ms-todo-skill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

```console
$ todo.py ls --today --all-lists
ST   ID            LIST    DUE         !  TITLE                 NOTES
[ ]  AAMkAGI2TGuz  Admin   today       !  Renew passport        reminder #admin
[~]  AAMkAGI2Wm4x  Work    today          Review PR 412         2/5
[ ]  AAMkAGI2Qp8v  Home    yesterday      Water the plants      repeats

$ todo.py add "Book dentist" --due friday --importance high
added AAMkAGI2Rr3  Book dentist  → Tasks

$ todo.py done "Book dentist"
completed AAMkAGI2Rr3  Book dentist
```

**Zero dependencies.** Standard library only — no `pip install`, no virtualenv,
no vendored SDK. Clone it and run it.

---

## Why this exists

Microsoft To Do has no official CLI, and the Graph SDKs are heavy for what is
fundamentally a handful of REST calls. This gives you the whole To Do surface —
lists, tasks, subtasks, reminders, recurrence, attachments, linked resources,
delta sync — behind a command line that is pleasant to use by hand and
predictable enough to put in a cron job.

It doubles as a [Claude Code](https://claude.com/claude-code) skill: drop it in
`~/.claude/skills/` and Claude can manage your tasks directly.

## Install

```bash
git clone https://github.com/byte-ish/ms-todo-skill.git
cd ms-todo-skill
python3 scripts/todo.py --help
```

Optionally, put it on your `PATH`:

```bash
ln -s "$PWD/scripts/todo.py" ~/.local/bin/todo
```

### As a Claude Code skill

```bash
git clone https://github.com/byte-ish/ms-todo-skill.git ~/.claude/skills/ms-todo
```

Claude picks it up from `SKILL.md` on the next session. Ask it things like
*"what's overdue?"*, *"add renew passport to my admin list, due Friday"*, or
*"break the release task into subtasks"*.

## Sign in

You need a Microsoft Entra public-client app registration with the delegated
`Tasks.ReadWrite` scope — about three minutes of clicking, walked through in
[`references/setup.md`](references/setup.md).

```bash
python3 scripts/todo.py auth login --client-id <application-client-id> --save
```

It prints a code and a URL. Approve in any browser, on any device — no redirect
URI, no client secret, works fine over SSH. The token lands in
`~/.config/ms-todo/token.json` at mode 0600 and refreshes itself from then on.

## Use it

```bash
todo.py lists ls                                   # your task lists
todo.py ls --overdue --all-lists                   # what's late, everywhere
todo.py ls -l Work --week --sort importance        # this week, most important first

todo.py add "Renew passport" --due friday --importance high -c admin
todo.py add "Ship v2" -l Work --due +2w --checklist "freeze branch" --checklist "tag"
todo.py add "Standup notes" -l Work --recur weekdays --due tomorrow

todo.py get "Renew passport"                       # full detail view
todo.py update "Renew passport" --due "next monday"
todo.py done "Renew passport"
todo.py move "Renew passport" --to Admin

todo.py delta -l Work                              # only what changed since last time
todo.py raw GET /me/todo/lists                     # escape hatch
```

You almost never need an id: `-l` takes a list name or fragment, and task
arguments take a title, a fragment, or an id prefix. Ambiguity is an error that
shows you the candidates.

### Scripting

`--json` emits raw Graph objects; `--jsonl` emits one per line.

```bash
todo.py --json ls --overdue --all-lists | jq -r '.[] | "\(._listName): \(.title)"'
```

Exit codes are a stable contract: `0` ok, `1` failure, `2` usage, `3` sign-in
required, `4` not found, `5` throttled, `6` missing permission.

```bash
todo.py --json ls --today > today.json
[ $? -eq 3 ] && echo "needs re-authentication"
```

Every mutation supports `--dry-run`, which prints the exact request it would send
and sends nothing.

---

## What it covers

| Area | Support |
| --- | --- |
| Task lists | list, get, create, rename, delete |
| Tasks | list, get, create, update, complete, reopen, delete, move |
| Filtering | status, importance, due windows, overdue, categories, text search |
| Subtasks | list, add, check, uncheck, delete |
| Reminders | absolute and relative, with sane 09:00 defaults |
| Recurrence | daily, weekdays, weekly, biweekly, monthly, yearly, with end date or count |
| Categories | on create and update |
| Attachments | inline under 3 MB, chunked upload session above |
| Linked resources | deep links back to your own systems |
| Delta sync | per-list tokens, additions, updates and removals |
| Escape hatch | `raw` for any Graph path |

Built-in reliability: `Retry-After`-aware throttling handling, exponential
backoff with jitter, automatic token refresh with a single 401 retry, automatic
`@odata.nextLink` paging, and typed errors that explain what to do next.

## Two things that will save you an afternoon

**Due dates are sent as midnight UTC by default.** To Do renders `dueDateTime` as
a bare calendar date. Sending midnight in a non-UTC zone makes clients east or
west of UTC display the neighbouring day — the single most common bug in To Do
integrations. Pass `--due-tz` only when the time of day genuinely matters.

**There is no app-only mode.** The To Do API's write endpoints are
delegated-only; `Application: Not supported` is right there in Microsoft's own
permission tables. A signed-in user is mandatory, which is exactly why this uses
the device code flow rather than client credentials.

## Documentation

- [`references/setup.md`](references/setup.md) — app registration, environment variables, sovereign clouds, troubleshooting
- [`references/api-reference.md`](references/api-reference.md) — every command and flag, plus the Graph resources underneath
- [`references/recipes.md`](references/recipes.md) — triage loops, cron jobs, mirroring an external system, using the package as a library
- [`SKILL.md`](SKILL.md) — the Claude Code skill definition

## Requirements

Python 3.9 or newer. Nothing else. On slim Linux images without a system tz
database, `pip install tzdata` for non-UTC timezone support.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest      # 202 tests, no network
.venv/bin/ruff check .
```

The test suite stubs HTTP at the `urlopen` boundary and isolates the config
directory, so it never touches the network or your real token cache.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Microsoft. "Microsoft", "Microsoft To Do",
"Outlook" and "Microsoft Graph" are trademarks of Microsoft Corporation.
