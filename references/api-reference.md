# Reference

Every command, every flag, and the Graph surface underneath it. Verified against
Microsoft Graph **v1.0** (August 2026).

All examples assume `todo.py` means `python3 scripts/todo.py`.

---

## Global flags

Available on every command, and they go *before* the subcommand:

| Flag | Effect |
| --- | --- |
| `--json` | print raw Graph objects instead of a table |
| `--jsonl` | one JSON object per line, for streaming into `jq` or a pipeline |
| `-q`, `--quiet` | suppress progress lines |
| `-v`, `-vv` | INFO / DEBUG logging to stderr |
| `--dry-run` | print writes instead of sending them; reads still happen |
| `-y`, `--yes` | skip the confirmation prompt on destructive commands |
| `--tz ZONE` | IANA zone for parsing and display (default: system zone) |
| `--client-id`, `--tenant` | override the stored credentials for one call |
| `--timeout`, `--retries` | per-request timeout and attempt count |
| `--no-cache` | bypass the 5-minute task-list index cache |
| `--color` / `--no-color` | force or disable colour (auto-detected otherwise) |

```bash
todo.py --json --tz Europe/London ls --overdue     # correct
todo.py ls --overdue --json                        # wrong: --json is global
```

---

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | generic failure (unexpected Graph error, network exhausted) |
| 2 | usage or configuration error, including ambiguous references |
| 3 | not signed in, or the identity platform refused the token |
| 4 | list, task or item does not exist |
| 5 | throttled by Graph and retries were exhausted |
| 6 | Graph returned 403 — missing scope or unconsented permission |

These are a stable contract. Branch on them instead of matching stderr text.

---

## Referring to lists and tasks

**`-l` / `--list`** accepts, in priority order:

1. an exact list id
2. a well-known alias: `default`, `tasks`, `inbox` → the built-in *Tasks* list;
   `flagged` → *Flagged email*
3. an exact display name, case-insensitive
4. a unique substring of a display name

Omitted entirely, it targets the built-in *Tasks* list.

**Task arguments** accept, in priority order:

1. an exact task id
2. a unique id prefix
3. an exact title, case-insensitive
4. a unique title substring

Anything matching more than one object is exit code 2 with the candidates
listed. Completed tasks are included in reference resolution even though they are
hidden from `ls` by default — you can still `undone` something you finished.

---

## Date expressions

Accepted by `--due`, `--start`, `--reminder`, `--due-before`, `--due-after`,
`--recur-until`:

| Form | Examples |
| --- | --- |
| Keywords | `today`, `tod`, `tomorrow`, `tmr`, `yesterday`, `tonight` (20:00) |
| End markers | `eod` (17:00 today), `eow` (17:00 Friday) |
| Weekdays | `friday`, `fri`, `next monday` — always the **next** such day, never today |
| Relative | `+3d`, `-1d`, `2w`, `4h`, `30m`, `in 4 hours`, `in 2 weeks` |
| Calendar | `next week` (next Monday), `next month` (1st of next month) |
| ISO | `2026-09-01`, `2026/09/01`, `2026-09-01T09:30`, `2026-09-01 09:30` |
| Clock only | `17:00`, `5pm`, `5:30pm` — today, or tomorrow if already past |
| Combined | `tomorrow 5pm`, `friday at 09:00`, `2026-09-01 17:00` |

### The timezone rule

`dueDateTime` is sent as **midnight UTC** unless you pass `--due-tz`. To Do
renders a due date as a bare calendar date; midnight in `Asia/Kolkata` is
18:30 UTC the previous day, and clients then disagree about which day the task is
due. This is the most common bug in To Do integrations and the default avoids it.

`--reminder` and `--start` are sent in your zone, because those are real
instants. A reminder given without a clock time defaults to **09:00** rather than
midnight.

---

## auth

```bash
todo.py auth login [--client-id ID] [--tenant T] [--save] [--force]
todo.py auth status
todo.py auth refresh
todo.py auth logout
```

`login` runs the device code flow: it prints a code and a URL, then polls until
you approve. `--save` persists client id, tenant and timezone to
`config.json`. `--force` re-authenticates even when a valid token is cached.

`status` exits 3 when signed out, which makes it a usable precondition check:

```bash
todo.py auth status >/dev/null || { echo "sign in first"; exit 1; }
```

Endpoints: `POST /{tenant}/oauth2/v2.0/devicecode`, `POST /{tenant}/oauth2/v2.0/token`.

---

## lists

```bash
todo.py lists ls
todo.py lists get <LIST>
todo.py lists new <NAME>
todo.py lists rename <LIST> <NEW_NAME>
todo.py lists rm <LIST> [--yes]
```

`lists rm` refuses to delete a built-in list (`defaultList`, `flaggedEmails`) —
Graph would reject it anyway, and the local check gives a better message. It
counts the tasks it is about to destroy before asking.

Graph: `GET|POST /me/todo/lists`, `GET|PATCH|DELETE /me/todo/lists/{id}`.

### todoTaskList fields

| Field | Type | Notes |
| --- | --- | --- |
| `id` | String | read-only, unique in the mailbox |
| `displayName` | String | the only writable field |
| `isOwner` | Boolean | false for lists shared with you |
| `isShared` | Boolean | shared with other users |
| `wellknownListName` | enum | `none`, `defaultList`, `flaggedEmails` |

---

## ls

```bash
todo.py ls [-l LIST] [-a] [filters] [--sort FIELD] [--reverse] [--limit N]
```

| Flag | Effect |
| --- | --- |
| `-a`, `--all-lists` | search every list; each task gains `_listName` and `_listId` |
| `--status STATUS` | exact status match; accepts aliases (see below) |
| `--include-completed` | keep completed tasks (hidden by default) |
| `--importance LEVEL` | `low`, `normal`, `high` |
| `--overdue` | due before midnight today |
| `--today` | due today or earlier |
| `--week` | due within seven days |
| `--due-before WHEN`, `--due-after WHEN` | arbitrary window |
| `--no-due` | only tasks with no due date |
| `-c`, `--category NAME` | repeatable; matches any |
| `-s`, `--search TEXT` | substring of title, body or categories |
| `--checklist` | expand checklist items (adds an `$expand`) |
| `--filter ODATA` | raw `$filter` passed to Graph |
| `--sort` | `due` (default), `created`, `modified`, `title`, `importance`, `status` |
| `--limit N` | cap the result count |

Filtering runs **client-side** after fetching pages. Graph's `$filter` support on
this endpoint is narrow and inconsistent between properties, so pushing
predicates down is unreliable; `--filter` is there for when you know a particular
expression works. Sorting is likewise local, and tasks with no value for the sort
field always land last rather than first.

### Table columns

```
ST   ID            DUE        !  TITLE              NOTES
[ ]  AAMkAGI2TGuz  tomorrow   !  Renew passport     reminder #admin 2/5
```

`ST` is the status glyph: `[ ]` notStarted, `[~]` inProgress, `[x]` completed,
`[w]` waitingOnOthers, `[>]` deferred. `!` marks high importance, `v` low.
`NOTES` collects `repeats`, `reminder`, `attach`, a checklist `done/total` when
`--checklist` is on, and `#category` tags. Overdue titles are red.

---

## add

```bash
todo.py add <TITLE...> [-l LIST] [field flags]
```

| Flag | Effect |
| --- | --- |
| `--due WHEN` | due date |
| `--due-tz ZONE` | send the due date in this zone instead of midnight UTC |
| `--start WHEN` | start date |
| `--reminder WHEN` | reminder; sets `isReminderOn` |
| `--importance LEVEL` | `low`, `normal`, `high` (aliases: `!`, `urgent`, `medium`) |
| `--status STATUS` | initial status |
| `-n`, `--note TEXT` | task body |
| `-c`, `--category NAME` | repeatable, or comma-separated |
| `--recur SPEC` | see below |
| `--recur-until WHEN` / `--recur-count N` | mutually exclusive |
| `--checklist TEXT` | add a subtask; repeatable |
| `--link URL` | add a linked resource; repeatable |
| `--link-app NAME` | `applicationName` for `--link` (default `ms-todo-skill`) |
| `--attach PATH` | attach a file; repeatable |
| `--if-not-exists` | no-op if a task with this exact title already exists in the list |

The title is variadic, so quoting is optional: `todo.py add Buy more milk` works.

`--if-not-exists` makes the command idempotent, which is what you want in a cron
job or a sync script. It matches on exact title within the target list only.

Graph: `POST /me/todo/lists/{listId}/tasks`.

### Recurrence specs

| Spec | Produces |
| --- | --- |
| `daily` | every day |
| `daily:3` | every third day |
| `weekdays` | Monday–Friday |
| `weekly` | weekly on the due date's weekday |
| `weekly:mon,thu` | weekly on the named days |
| `biweekly` / `biweekly:fri` | every two weeks |
| `monthly` | monthly on the due date's day of month |
| `monthly:15` | monthly on the 15th |
| `yearly` | annually on the due date's month and day |

The recurrence range anchors on `--due` when present, otherwise today. Add
`--recur-until 2026-12-31` for an end date or `--recur-count 10` for a fixed
number of occurrences; without either, it never ends.

Completing a recurring task makes Graph generate the next occurrence
server-side. That is To Do's own behaviour.

---

## get, update, done, undone, start, rm, move

```bash
todo.py get <TASK> [-l LIST]
todo.py update <TASK> [-l LIST] [field flags] [--clear-* flags]
todo.py done <TASK...>      [-l LIST]
todo.py undone <TASK...>    [-l LIST]
todo.py start <TASK...>     [-l LIST]
todo.py rm <TASK...>        [-l LIST] [--yes]
todo.py move <TASK> --to <LIST> [-l LIST]
```

`get` expands checklist items and linked resources and prints a detail view.

`update` takes every `add` field plus `--title`, and a set of clearing flags:
`--clear-due`, `--clear-start`, `--clear-reminder`, `--clear-recurrence`,
`--clear-categories`. Clearing sends an explicit `null`, which is how Graph
removes a value; fields you don't mention are absent from the PATCH entirely.
An update with no fields at all is exit code 2, not a silent no-op.

`done`, `undone` and `start` accept several tasks and are just status PATCHes
(`completed`, `notStarted`, `inProgress`).

`move` has no Graph equivalent — ids are list-scoped and the API offers no move.
It copies the task into the destination and deletes the original, so **the id
changes**. Checklist items and linked resources are recreated; attachments and
open extensions are not carried over.

Graph: `GET|PATCH|DELETE /me/todo/lists/{listId}/tasks/{taskId}`.

### todoTask fields

| Field | Type | Notes |
| --- | --- | --- |
| `id` | String | changes when the task moves between lists |
| `title` | String | |
| `body` | itemBody | `{content, contentType}`; this tool writes `text` |
| `status` | enum | `notStarted`, `inProgress`, `completed`, `waitingOnOthers`, `deferred` |
| `importance` | enum | `low`, `normal`, `high` |
| `dueDateTime` | dateTimeTimeZone | rendered as a calendar date by To Do |
| `startDateTime` | dateTimeTimeZone | |
| `reminderDateTime` | dateTimeTimeZone | paired with `isReminderOn` |
| `completedDateTime` | dateTimeTimeZone | set by the service |
| `isReminderOn` | Boolean | |
| `recurrence` | patternedRecurrence | `{pattern, range}` |
| `categories` | String collection | must match an existing Outlook category name to show colour |
| `hasAttachments` | Boolean | read-only |
| `createdDateTime`, `lastModifiedDateTime`, `bodyLastModifiedDateTime` | DateTimeOffset | read-only, always UTC |

Status aliases accepted on input: `todo`/`open` → `notStarted`,
`doing`/`wip`/`in-progress` → `inProgress`, `done`/`complete` → `completed`,
`waiting`/`blocked` → `waitingOnOthers`, `later`/`someday` → `deferred`.

---

## checklist

```bash
todo.py checklist ls      <TASK> [-l LIST]
todo.py checklist add     <TASK> <ITEM...> [-l LIST]
todo.py checklist check   <TASK> <ITEM...> [-l LIST]
todo.py checklist uncheck <TASK> <ITEM...> [-l LIST]
todo.py checklist rm      <TASK> <ITEM...> [-l LIST]
```

Aliased as `sub`. Items are referenced by id or by a unique substring of their
display name.

Graph: `/me/todo/lists/{listId}/tasks/{taskId}/checklistItems[/{itemId}]`.
Fields: `id`, `displayName`, `isChecked`, `checkedDateTime`, `createdDateTime`.

---

## link

```bash
todo.py link ls  <TASK> [-l LIST]
todo.py link add <TASK> [--url URL] [--name NAME] [--app APP] [--external-id ID]
todo.py link rm  <TASK> <LINK> [-l LIST]
```

A `linkedResource` points a task back at wherever it came from — an email, a
ticket, a row in your own system. It shows in the To Do task detail pane. `webUrl`
is optional: a link with only `externalId` renders without a hyperlink, which is
right for items that have no web address.

Graph: `/me/todo/lists/{listId}/tasks/{taskId}/linkedResources[/{id}]`.
Fields: `id`, `displayName`, `applicationName`, `webUrl`, `externalId`.

---

## attach

```bash
todo.py attach ls  <TASK> [-l LIST]
todo.py attach add <TASK> <PATH...> [--name NAME] [-l LIST]
todo.py attach get <TASK> <ATTACHMENT> [-o PATH] [-l LIST]
todo.py attach rm  <TASK> <ATTACHMENT> [--yes] [-l LIST]
```

Files under 3 MB are posted inline as base64. Larger files automatically
negotiate an upload session and stream in 3.125 MiB chunks (a multiple of the
320 KiB Graph requires). The upload URL is pre-authenticated, so no bearer token
is sent to it.

`attach get` only works for inline attachments; content uploaded through a
session is not returned in the `contentBytes` property.

Graph: `/me/todo/lists/{listId}/tasks/{taskId}/attachments`,
`.../attachments/createUploadSession`.

---

## delta

```bash
todo.py delta [-l LIST] [--reset] [--run]
```

Incremental sync. The first call returns every task in the list plus a delta
token, stored per-list in `~/.config/ms-todo/delta-state.json`. Later calls
return only what changed since then.

Deleted tasks come back as stubs carrying `@removed`; they are reported
separately as `removed` / `removedIds` rather than mixed into the changes.

`--reset` forgets the stored token so the next run is a full snapshot again; add
`--run` to reset and fetch in one go.

Graph: `GET /me/todo/lists/{listId}/tasks/delta`. Delta supports `$select`,
`$top` and `$expand`; it does **not** support `$search`, and its `$filter` and
`$orderby` support is limited to `receivedDateTime`. Query parameters are baked
into the returned tokens, so they only need to be given on the first call.

---

## raw

```bash
todo.py raw GET /me/todo/lists
todo.py raw POST /me/todo/lists --data '{"displayName":"Scratch"}'
todo.py raw PATCH /me/todo/lists/{id}/tasks/{id} --data @patch.json
echo '{"status":"completed"}' | todo.py raw PATCH /me/todo/lists/{id}/tasks/{id} --data -
```

The escape hatch: any Graph path with the cached token, retries and error
handling applied. `--data` takes inline JSON, `@filename`, or `-` for stdin. The
response is printed as JSON.

The cached token only carries `Tasks.ReadWrite`, so paths outside the To Do
surface will come back as 403.

---

## Reliability behaviour

**Retries.** 408, 429, 500, 502, 503 and 504 are retried, as are network-level
failures. 429 and 503 honour `Retry-After` (both delta-seconds and HTTP-date
forms), capped at 120 seconds so a bad gateway cannot hang your terminal.
Everything else uses exponential backoff with jitter, capped at 60 seconds.
4xx responses other than 408 and 429 are never retried.

**Token refresh.** Access tokens are refreshed 120 seconds before expiry. A 401
from Graph triggers one forced refresh and one retry; a second 401 is a real
failure. Refresh tokens rotate — the newest one is always what gets stored.

**Paging.** `@odata.nextLink` is followed automatically. Query parameters are
sent on the first request only, because Graph encodes them into the nextLink and
re-sending them can conflict.

**Caching.** The task-list index is cached for five minutes at
`~/.config/ms-todo/lists-cache.json`, since resolving a list by name would
otherwise cost a request per command. It is invalidated on any list mutation, and
a resolution failure forces one refresh before giving up. `--no-cache` skips it.

---

## Limits worth knowing

| Thing | Limit |
| --- | --- |
| Inline attachment size | under 3 MB; larger needs an upload session |
| Device code validity | 15 minutes |
| Access token lifetime | ~1 hour, refreshed automatically |
| National clouds | Global, US Gov L4, US Gov L5 supported; China (21Vianet) is not |
| App-only access | not supported for To Do at all |
| `$search` on tasks | not supported; `--search` filters client-side |
