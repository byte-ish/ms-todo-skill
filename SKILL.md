---
name: ms-todo
description: Use when working with Microsoft To Do — creating, listing, updating, completing, moving or deleting tasks and task lists, managing checklist items (subtasks), reminders, due dates, recurrence, categories, file attachments and linked resources, or syncing changes with delta queries. Triggers on "Microsoft To Do", "MS To Do", "my To Do list", "add a task", "what's due today", "what's overdue", "mark that done", or any request to automate Microsoft/Outlook tasks from a script or the command line. Talks to the Microsoft Graph v1.0 To Do API with a device-code sign-in.
license: MIT
---

# Microsoft To Do

Drive Microsoft To Do over the Microsoft Graph v1.0 API using `scripts/todo.py` —
stdlib-only Python, no install step.

## Do the work, don't print instructions

When a request can be executed, run the script. Only emit code or curl when the
user asked for something *they* will run (a cron job, a CI step, a teammate's
script). Never hand back a command as a substitute for doing the task.

```bash
python3 scripts/todo.py <command> [options]
```

Run `auth status` once at the start of a session that will do writes. Exit code 3
means "not signed in" — tell the user to run `auth login` (it needs a browser on
some device, so you cannot complete it for them).

---

## First run

The user needs a Microsoft Entra **public client** app registration with the
delegated `Tasks.ReadWrite` scope and *Allow public client flows* enabled.
`references/setup.md` is the click-by-click walkthrough — point them at it rather
than improvising Azure portal steps.

```bash
python3 scripts/todo.py auth login --client-id <app-id> --save
```

This prints a code and a URL; the user approves in any browser, on any device.
The token is cached at `~/.config/ms-todo/token.json` with mode 0600 and is
refreshed automatically from then on.

**Never** print the token, pass it as a CLI argument, or copy it into a file.

---

## The commands you will actually use

| Task | Command |
| --- | --- |
| What's on today | `todo.py ls --today --all-lists` |
| What's overdue | `todo.py ls --overdue --all-lists` |
| Everything in a list | `todo.py ls -l "Work"` |
| Add a task | `todo.py add "Renew passport" --due friday --importance high` |
| Add with subtasks | `todo.py add "Trip" --checklist "book flight" --checklist "pack"` |
| Recurring task | `todo.py add "Standup notes" --recur weekdays --due tomorrow` |
| Complete a task | `todo.py done "Renew passport"` |
| Reschedule | `todo.py update "Renew passport" --due "next monday"` |
| Move between lists | `todo.py move "Renew passport" --to "Admin"` |
| Show full detail | `todo.py get "Renew passport"` |
| Task lists | `todo.py lists ls` |
| Incremental sync | `todo.py delta -l "Work"` |
| Anything else | `todo.py raw GET /me/todo/lists` |

Full reference: `references/api-reference.md`. Worked multi-step workflows:
`references/recipes.md`.

### Referring to tasks and lists

You rarely need ids. `-l/--list` accepts an id, a display name, a unique name
fragment, or `default`/`flagged`. A task argument accepts an id, a unique id
prefix, an exact title, or a unique title fragment. Ambiguity is an error that
lists the candidates — read it and retry with something more specific rather
than guessing.

Omitting `--list` targets the built-in **Tasks** list, which is where To Do
itself puts anything added without choosing a list.

### Dates

`--due`, `--start`, `--reminder`, `--due-before`, `--due-after` and
`--recur-until` all take: `today`, `tomorrow`, `tonight`, `yesterday`, a weekday
(`friday`, `next monday` — always the *next* one, never today), `eod`, `eow`,
offsets (`+3d`, `2w`, `in 4 hours`), ISO dates (`2026-09-01`), or a date plus a
time (`tomorrow 5pm`, `2026-09-01 17:00`).

---

## Machine-readable output

`--json` prints the raw Graph objects, unmodified. `--jsonl` prints one object
per line. Use these whenever you need to read a value back rather than show it
to the user:

```bash
python3 scripts/todo.py --json ls --overdue --all-lists
```

Every listed task also carries `_listName` and `_listId` so you know where it
came from without a second call.

## Exit codes

`0` ok · `1` failure · `2` usage or config · `3` sign-in required · `4` not found
· `5` throttled after retries · `6` missing Graph permission.

Branch on these rather than parsing stderr.

---

## Things that will bite you

**Due dates are sent as midnight UTC by default.** To Do renders `dueDateTime`
as a bare calendar date; sending midnight in a non-UTC zone makes clients east or
west of UTC show the neighbouring day. Only pass `--due-tz` when the time of day
genuinely matters.

**There is no move operation in the API.** `move` copies the task to the target
list and deletes the original, so **the task id changes**. Checklist items and
linked resources are carried across; attachments and open extensions are not.
Say so if the user is tracking ids.

**Filtering happens client-side.** The To Do endpoint's `$filter` support is
narrow and inconsistent, so `--overdue`, `--search`, `--category` and friends
fetch pages and filter locally. On very large lists prefer `-l` over
`--all-lists`. `--filter` passes raw OData through if you know it works.

**Deleting is permanent** — no recycle bin. `rm` and `lists rm` refuse to run
without `--yes` when stdin is not a terminal. Confirm with the user before
passing `--yes` on their behalf.

**Completing a recurring task creates the next occurrence** server-side. That is
To Do's behaviour, not a bug in this skill.

**Only delegated permissions exist for writes.** There is no app-only mode for
To Do; a signed-in user is mandatory. Do not suggest client credentials.

Use `--dry-run` on any mutation to see the exact request without sending it.
Reach for it when you are unsure whether a bulk operation will do what the user
meant.
