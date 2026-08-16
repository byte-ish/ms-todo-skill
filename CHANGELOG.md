# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Exit codes are part of the public contract: changing one is a major version bump.

## [Unreleased]

## [1.0.0] - 2026-08-16

First release.

### Added

- **Authentication** — OAuth 2.0 device authorization grant against the Microsoft
  identity platform. Token cache at `~/.config/ms-todo/token.json` written 0600
  and atomically, automatic refresh 120s before expiry, refresh-token rotation,
  and invalidation when the configured client id or tenant changes.
- **Task lists** — list, get, create, rename and delete, with built-in lists
  protected from deletion.
- **Tasks** — list, get, create, update, complete, reopen, mark in progress,
  delete, and move between lists.
- **Reference resolution** — lists and tasks addressable by id, id prefix,
  well-known alias, exact name, or unique name fragment, with ambiguity reported
  as an error that names the candidates.
- **Filtering and sorting** — status, importance, due windows (`--overdue`,
  `--today`, `--week`, `--due-before`, `--due-after`, `--no-due`), categories and
  full-text search, sorted by due date, creation, modification, title, importance
  or status.
- **Checklist items** — list, add, check, uncheck and delete subtasks.
- **Linked resources** — create and remove deep links back to source systems.
- **Attachments** — inline upload under 3 MB, automatic chunked upload session
  above it, plus listing, download and deletion.
- **Recurrence** — `daily`, `daily:N`, `weekdays`, `weekly[:days]`, `biweekly`,
  `monthly[:day]` and `yearly`, with optional end date or occurrence count.
- **Delta sync** — per-list delta tokens persisted between runs, with removals
  reported separately from changes.
- **Date parsing** — keywords, weekday names, `eod`/`eow`, relative offsets, ISO
  dates and combined date-and-time expressions, timezone aware.
- **`raw`** — arbitrary Graph calls using the cached token, with the same retry
  and error handling as every other command.
- **Output modes** — human-readable tables with colour, `--json` for raw Graph
  objects, `--jsonl` for streaming.
- **Reliability** — `Retry-After`-aware handling of 429 and 503 (both
  delta-seconds and HTTP-date forms, capped), exponential backoff with jitter on
  transient 5xx and network failures, one forced token refresh and retry on 401,
  and automatic `@odata.nextLink` paging.
- **Safety** — `--dry-run` on every mutation, confirmation required for
  destructive commands, and a refusal to run them non-interactively without
  `--yes`.
- **Typed exit codes** — 0 ok, 1 failure, 2 usage or config, 3 sign-in required,
  4 not found, 5 throttled, 6 missing permission.
- Claude Code skill definition in `SKILL.md`.
- 202 unit tests covering date parsing, retry policy, the device code flow,
  Graph error mapping, paging, reference resolution and the CLI surface. No test
  touches the network.

### Notes

- Due dates are sent as **midnight UTC** by default. To Do renders `dueDateTime`
  as a bare calendar date, and sending midnight in a non-UTC zone makes clients
  either side of UTC disagree about the day. Use `--due-tz` to override.
- `move` is implemented as copy-then-delete because the Graph API has no move
  operation for `todoTask`, so **the task id changes**. Checklist items and
  linked resources are carried across; attachments and open extensions are not.
- Filtering runs client-side: the To Do endpoint's `$filter` support is too
  narrow and inconsistent to rely on. `--filter` passes raw OData through for
  cases where you know an expression works.

[Unreleased]: https://github.com/byte-ish/ms-todo-skill/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/byte-ish/ms-todo-skill/releases/tag/v1.0.0
