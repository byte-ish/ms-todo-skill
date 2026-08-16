# Recipes

Worked multi-step workflows. `todo.py` means `python3 scripts/todo.py`.

---

## A morning briefing

```bash
todo.py ls --overdue --all-lists
todo.py ls --today --all-lists
```

As one machine-readable payload:

```bash
todo.py --json ls --week --all-lists --sort due
```

Count what's overdue, per list:

```bash
todo.py --jsonl ls --overdue --all-lists | jq -r '._listName' | sort | uniq -c | sort -rn
```

---

## Capture a task quickly

```bash
todo.py add Call the dentist                      # goes to the built-in Tasks list
todo.py add "Renew passport" --due friday --importance high -c admin
todo.py add "Review PR 412" -l Work --due today --link https://github.com/o/r/pull/412
```

With a reminder rather than just a date:

```bash
todo.py add "Leave for airport" --due 2026-09-14 --reminder "2026-09-14 05:30"
```

A reminder without a clock time defaults to 09:00, not midnight.

---

## Break a task into subtasks

```bash
todo.py add "Ship v2.0" -l Work --due "+2w" \
  --checklist "freeze the branch" \
  --checklist "run the migration on staging" \
  --checklist "write release notes" \
  --checklist "tag and publish"

todo.py checklist ls "Ship v2.0" -l Work
todo.py checklist check "Ship v2.0" "freeze" -l Work
todo.py ls -l Work --checklist          # shows a done/total column
```

---

## Recurring routines

```bash
todo.py add "Standup notes" -l Work --recur weekdays --due tomorrow
todo.py add "Water the plants" --recur weekly:tue,sat --due tomorrow
todo.py add "Pay rent" --recur monthly:1 --due 2026-09-01
todo.py add "Renew domain" --recur yearly --due 2026-11-03 --recur-count 5
```

Stop something recurring without deleting its history:

```bash
todo.py update "Water the plants" --clear-recurrence
```

---

## Triage: reschedule everything overdue

Push every overdue task to tomorrow, in one list:

```bash
todo.py --jsonl ls --overdue -l Work \
  | jq -r '.id' \
  | while read -r id; do todo.py update "$id" -l Work --due tomorrow; done
```

Check what it would do first — `--dry-run` prints the exact request body without
sending it:

```bash
todo.py --dry-run update "Renew passport" --due tomorrow
```

---

## Bulk close

```bash
todo.py done "Renew passport" "Call the dentist" "Review PR 412"
```

Everything matching a search, with a look before you leap:

```bash
todo.py ls -l Work -s "sprint 41"            # eyeball it
todo.py --jsonl ls -l Work -s "sprint 41" \
  | jq -r '.id' \
  | xargs -r -n1 todo.py done -l Work
```

---

## Move a task between lists

```bash
todo.py move "Renew passport" --to Admin
```

Remember this is a copy-and-delete: the id changes, and attachments do not come
along. If you are storing ids anywhere, re-read the new one from the output:

```bash
NEW_ID=$(todo.py --json move "Renew passport" --to Admin | jq -r '.id')
```

---

## Link tasks back to your own system

Give the task a deep link so the To Do app can jump back to the source:

```bash
todo.py add "Fix flaky test in CI" -l Work \
  --link "https://ci.example.com/build/8821" --link-app "Example CI"
```

For a system with no web UI, record the identifier alone:

```bash
todo.py link add "Fix flaky test in CI" -l Work \
  --name "build 8821" --app "Example CI" --external-id "8821"
```

---

## Attachments

```bash
todo.py attach add "Renew passport" ~/Documents/form-DS11.pdf
todo.py attach ls "Renew passport"
todo.py attach get "Renew passport" AAMkAD... -o ./form.pdf
```

Anything over 3 MB switches to a chunked upload session automatically. Files
uploaded that way can't be downloaded back through `attach get` — fetch them from
the To Do app.

---

## Mirror an external system into To Do

The idempotent pattern. `--if-not-exists` means re-running is safe:

```bash
#!/usr/bin/env bash
set -euo pipefail

todo.py auth status >/dev/null || { echo "run: todo.py auth login" >&2; exit 1; }
todo.py --json lists ls | jq -e '.[] | select(.displayName=="Incidents")' >/dev/null \
  || todo.py lists new Incidents

fetch_open_incidents | jq -r '.[] | [.id, .title, .due] | @tsv' \
| while IFS=$'\t' read -r id title due; do
    todo.py add "$title" -l Incidents --due "$due" --if-not-exists \
      --link "https://incidents.example.com/$id" --link-app "Incidents"
  done
```

---

## One-way sync out of To Do

`delta` gives you only what changed since last time:

```bash
todo.py --json delta -l Work > changes.json
jq '{changed, removed, titles: [.upserts[].title]}' changes.json
```

The delta token is stored per list, so this is safe to run on a schedule. Start
over with a full snapshot:

```bash
todo.py delta -l Work --reset --run
```

A polling loop that only acts when something moved:

```bash
while sleep 300; do
  payload=$(todo.py --json delta -l Work)
  count=$(jq -r '.changed + .removed' <<<"$payload")
  [ "$count" -gt 0 ] && notify_downstream "$payload"
done
```

---

## Weekly review

```bash
# Finished this week
todo.py --json ls --all-lists --status done --include-completed \
  | jq -r '.[] | select(.completedDateTime.dateTime > "2026-08-10") | .title'

# Never given a due date
todo.py ls --all-lists --no-due

# Stalled: waiting on someone else
todo.py ls --all-lists --status blocked
```

---

## Running from cron

Sign in once interactively with the config directory the job will use, then:

```cron
0 8 * * 1-5 MSTODO_CONFIG_DIR=/opt/ms-todo /usr/bin/python3 /opt/ms-todo-skill/scripts/todo.py \
  --json --quiet ls --today --all-lists > /var/log/todo-today.json 2>/var/log/todo.err
```

Handle expiry properly — exit code 3 means a human must sign in again, and
retrying will not help:

```bash
todo.py --json ls --today --all-lists > out.json
case $? in
  0) ;;
  3) alert "ms-todo needs re-authentication" ;;
  5) alert "ms-todo throttled; backing off" ;;
  *) alert "ms-todo failed unexpectedly" ;;
esac
```

---

## Using the package directly

The CLI is a thin shell over an importable library:

```python
import sys
sys.path.insert(0, "scripts")

from mstodo.auth import DeviceCodeAuth
from mstodo.config import Config
from mstodo.graph import GraphClient
from mstodo.service import TodoService, filter_tasks

config = Config()
service = TodoService(GraphClient(DeviceCodeAuth(config), config))

work = service.resolve_list("Work")
overdue = filter_tasks(service.iter_tasks(work["id"]), due_before=datetime.now(timezone.utc))

for task in overdue:
    print(task["title"])
```

Errors are typed and carry their own exit codes — see `mstodo.errors`.
