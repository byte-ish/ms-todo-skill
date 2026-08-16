"""Command line interface.

Design rules worth knowing before extending this file:

* Every mutation supports ``--dry-run`` and prints what it would send.
* Destructive commands refuse to run non-interactively without ``--yes``.
* ``--json`` emits the raw Graph object, unmodified, so scripts can rely on it.
* Exit codes are defined in :mod:`mstodo.errors` and are part of the contract.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .auth import DeviceCodeAuth
from .config import Config, config_path, delta_state_path, read_json, write_json_private
from .dates import local_timezone_name, parse_when, zone
from .errors import MsTodoError, UsageError
from .format import (
    dump_json,
    dump_jsonl,
    render_lists,
    render_task_detail,
    render_tasks,
    short_id,
    use_color,
)
from .graph import GraphClient
from .models import TaskPayloadBuilder, normalize_importance, normalize_status
from .service import TodoService, filter_tasks, task_sort_key

log = logging.getLogger("mstodo")

SORT_FIELDS = ("due", "created", "modified", "title", "importance", "status")


class Context:
    """Lazily built dependencies, so ``--help`` never touches the network or disk."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = Config(
            client_id=getattr(args, "client_id", None),
            tenant=getattr(args, "tenant", None),
            timeout=getattr(args, "timeout", None),
            retries=getattr(args, "retries", None),
        )
        self.tz_name = args.tz or self.config.timezone or local_timezone_name()
        zone(self.tz_name)  # fail fast on a bad zone rather than mid-request
        self.color = use_color() if args.color is None else args.color
        self._auth: DeviceCodeAuth | None = None
        self._service: TodoService | None = None

    @property
    def auth(self) -> DeviceCodeAuth:
        if self._auth is None:
            self._auth = DeviceCodeAuth(self.config)
        return self._auth

    @property
    def service(self) -> TodoService:
        if self._service is None:
            client = GraphClient(
                self.auth,
                self.config,
                timezone=self.tz_name,
                dry_run=self.args.dry_run,
            )
            self._service = TodoService(client, use_cache=not self.args.no_cache)
        return self._service

    @property
    def now(self) -> dt.datetime:
        return dt.datetime.now(zone(self.tz_name))

    def emit(self, payload: Any, text: str | None = None) -> None:
        """Print JSON or human output according to the global flags."""
        if self.args.jsonl and isinstance(payload, list):
            dump_jsonl(payload)
        elif self.args.json or self.args.jsonl:
            dump_json(payload)
        elif text is not None:
            if text:
                print(text)
        else:
            dump_json(payload)

    def note(self, message: str) -> None:
        """Progress and confirmation text. Suppressed by --quiet and in JSON mode."""
        if not self.args.quiet and not (self.args.json or self.args.jsonl):
            print(message)

    def confirm(self, prompt: str) -> bool:
        if self.args.yes:
            return True
        if not sys.stdin.isatty():
            raise UsageError(
                "refusing to run a destructive command without confirmation",
                hint="pass --yes when running non-interactively",
            )
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in ("y", "yes")


# --------------------------------------------------------------------- auth


def cmd_auth_login(ctx: Context) -> int:
    args = ctx.args
    if args.save:
        ctx.config.save(client_id=args.client_id, tenant=args.tenant, timezone=args.tz)
        ctx.note(f"saved settings to {config_path()}")

    # Fail early with the full registration hint rather than mid-flow.
    _ = ctx.config.client_id

    if not args.force:
        status = ctx.auth.status()
        if status.get("signed_in") and status.get("access_token_expires_in", 0) > 0:
            ctx.note(f"already signed in as {status['account'].get('username') or 'unknown'}")
            ctx.note("pass --force to sign in again")
            return 0

    def show(prompt: Any) -> None:
        sys.stderr.write(prompt.render())
        sys.stderr.flush()

    account = ctx.auth.login(show)
    ctx.note(f"signed in as {account.label()}")
    ctx.note(f"token cached at {ctx.auth.cache.path} (0600)")
    return 0


def cmd_auth_status(ctx: Context) -> int:
    status = ctx.auth.status()
    if ctx.args.json or ctx.args.jsonl:
        dump_json(status)
        return 0 if status.get("signed_in") else 3
    if not status.get("signed_in"):
        print("not signed in")
        print("hint: run 'auth login'")
        return 3
    account = status.get("account") or {}
    print(f"signed in as {account.get('username') or account.get('name') or 'unknown'}")
    print(f"  tenant:     {status.get('tenant')}")
    print(f"  client id:  {status.get('client_id')}")
    print(f"  scopes:     {status.get('scope')}")
    print(f"  expires in: {status.get('access_token_expires_in')}s")
    print(f"  refresh:    {'yes' if status.get('has_refresh_token') else 'no'}")
    print(f"  token file: {status.get('token_file')}")
    return 0


def cmd_auth_logout(ctx: Context) -> int:
    removed = ctx.auth.logout()
    ctx.emit({"signedOut": removed}, "signed out" if removed else "was not signed in")
    return 0


def cmd_auth_refresh(ctx: Context) -> int:
    account = ctx.auth.refresh()
    ctx.emit(ctx.auth.status(), f"refreshed token for {account.label()}")
    return 0


# -------------------------------------------------------------------- lists


def cmd_lists_ls(ctx: Context) -> int:
    lists = ctx.service.list_lists(refresh=ctx.args.no_cache)
    ctx.emit(lists, render_lists(lists, color=ctx.color))
    return 0


def cmd_lists_get(ctx: Context) -> int:
    found = ctx.service.resolve_list(ctx.args.list_ref)
    ctx.emit(found, render_lists([found], color=ctx.color))
    return 0


def cmd_lists_new(ctx: Context) -> int:
    created = ctx.service.create_list(ctx.args.name)
    ctx.emit(created, f"created list {ctx.args.name!r} ({short_id(created.get('id'))})")
    return 0


def cmd_lists_rename(ctx: Context) -> int:
    found = ctx.service.resolve_list(ctx.args.list_ref)
    updated = ctx.service.rename_list(found["id"], ctx.args.name)
    ctx.emit(updated, f"renamed {found.get('displayName')!r} to {ctx.args.name!r}")
    return 0


def cmd_lists_rm(ctx: Context) -> int:
    found = ctx.service.resolve_list(ctx.args.list_ref)
    if found.get("wellknownListName") not in (None, "none"):
        raise UsageError(
            f"{found.get('displayName')!r} is a built-in list and cannot be deleted",
            hint="built-in lists are defaultList and flaggedEmails",
        )
    count = sum(1 for _ in ctx.service.iter_tasks(found["id"], limit=200))
    if not ctx.confirm(f"delete list {found.get('displayName')!r} and its {count}+ tasks?"):
        ctx.note("aborted")
        return 0
    ctx.service.delete_list(found["id"])
    ctx.emit({"deleted": found["id"]}, f"deleted list {found.get('displayName')!r}")
    return 0


# -------------------------------------------------------------------- tasks


def _collect_tasks(ctx: Context) -> list[dict[str, Any]]:
    args = ctx.args
    service = ctx.service

    targets = service.list_lists() if args.all_lists else [service.resolve_list(args.list_ref)]

    expand = ["checklistItems"] if getattr(args, "checklist", False) else None
    gathered: list[dict[str, Any]] = []
    for target in targets:
        for task in service.iter_tasks(target["id"], expand=expand, odata_filter=args.filter):
            task["_listName"] = target.get("displayName")
            task["_listId"] = target["id"]
            gathered.append(task)

    due_before = due_after = None
    start_of_today = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    if args.overdue:
        due_before = start_of_today
    if args.today:
        due_before = start_of_today + dt.timedelta(days=1)
    if args.week:
        due_before = start_of_today + dt.timedelta(days=7)
    if args.due_before:
        due_before = parse_when(args.due_before, now=ctx.now, tz_name=ctx.tz_name)[0]
    if args.due_after:
        due_after = parse_when(args.due_after, now=ctx.now, tz_name=ctx.tz_name)[0]

    has_due = True if (args.overdue or args.today or args.week) else None
    if args.no_due:
        has_due = False

    filtered = filter_tasks(
        gathered,
        status=normalize_status(args.status),
        include_completed=args.include_completed,
        importance=normalize_importance(args.importance),
        due_before=due_before,
        due_after=due_after,
        has_due=has_due,
        categories=args.category,
        query=args.search,
    )
    filtered.sort(key=lambda t: task_sort_key(t, args.sort), reverse=args.reverse)
    if args.limit:
        filtered = filtered[: args.limit]
    return filtered


def cmd_tasks_ls(ctx: Context) -> int:
    tasks = _collect_tasks(ctx)
    ctx.emit(
        tasks,
        render_tasks(tasks, color=ctx.color, now=ctx.now, show_list=ctx.args.all_lists),
    )
    return 0


def cmd_task_get(ctx: Context) -> int:
    target = ctx.service.resolve_list(ctx.args.list_ref)
    task = ctx.service.resolve_task(target["id"], ctx.args.task)
    full = ctx.service.get_task(
        target["id"], task["id"], expand=["checklistItems", "linkedResources"]
    )
    ctx.emit(full, render_task_detail(full, color=ctx.color))
    return 0


def cmd_task_add(ctx: Context) -> int:
    args = ctx.args
    target = ctx.service.resolve_list(args.list_ref)
    title = " ".join(args.title).strip()

    if args.if_not_exists:
        existing = ctx.service.find_by_title(target["id"], title)
        if existing:
            ctx.emit(existing, f"already exists: {short_id(existing['id'])}  {title}")
            return 0

    builder = TaskPayloadBuilder(tz_name=ctx.tz_name, due_tz=args.due_tz, now=ctx.now)
    builder.title(title).note(args.note).status(args.status).importance(args.importance)
    builder.categories(args.category).due(args.due).start(args.start).reminder(args.reminder)
    builder.recurrence(args.recur, until=args.recur_until, count=args.recur_count)

    created = ctx.service.create_task(target["id"], builder.build())
    if ctx.args.dry_run:
        ctx.emit(created, f"dry-run: would create {title!r} in {target.get('displayName')!r}")
        return 0

    for item in args.checklist or []:
        ctx.service.add_checklist_item(target["id"], created["id"], item)
    for url in args.link or []:
        ctx.service.add_link(
            target["id"],
            created["id"],
            display_name=url,
            application_name=args.link_app,
            web_url=url,
        )
    for path in args.attach or []:
        ctx.service.add_attachment(target["id"], created["id"], Path(path))

    ctx.emit(created, f"added {short_id(created.get('id'))}  {title}  → {target.get('displayName')}")
    return 0


def _apply_updates(ctx: Context) -> dict[str, Any]:
    args = ctx.args
    builder = TaskPayloadBuilder(tz_name=ctx.tz_name, due_tz=args.due_tz, now=ctx.now)
    builder.title(args.title).note(args.note).status(args.status).importance(args.importance)
    builder.categories(args.category).due(args.due).start(args.start).reminder(args.reminder)
    builder.recurrence(args.recur, until=args.recur_until, count=args.recur_count)

    if args.clear_due:
        builder.clear("dueDateTime")
    if args.clear_start:
        builder.clear("startDateTime")
    if args.clear_reminder:
        builder.clear("reminderDateTime")
        builder.set("isReminderOn", False)
    if args.clear_recurrence:
        builder.clear("recurrence")
    if args.clear_categories:
        builder.set("categories", [])
    return builder.build()


def cmd_task_update(ctx: Context) -> int:
    target = ctx.service.resolve_list(ctx.args.list_ref)
    task = ctx.service.resolve_task(target["id"], ctx.args.task)
    payload = _apply_updates(ctx)
    updated = ctx.service.update_task(target["id"], task["id"], payload)
    ctx.emit(updated, f"updated {short_id(task['id'])}  {task.get('title')}")
    return 0


def _bulk_status(ctx: Context, status: str, verb: str) -> int:
    target = ctx.service.resolve_list(ctx.args.list_ref)
    results = []
    for ref in ctx.args.task:
        task = ctx.service.resolve_task(target["id"], ref)
        results.append(ctx.service.set_status(target["id"], task["id"], status))
        ctx.note(f"{verb} {short_id(task['id'])}  {task.get('title')}")
    if ctx.args.json or ctx.args.jsonl:
        ctx.emit(results)
    return 0


def cmd_task_done(ctx: Context) -> int:
    return _bulk_status(ctx, "completed", "completed")


def cmd_task_undone(ctx: Context) -> int:
    return _bulk_status(ctx, "notStarted", "reopened")


def cmd_task_start(ctx: Context) -> int:
    return _bulk_status(ctx, "inProgress", "started")


def cmd_task_rm(ctx: Context) -> int:
    target = ctx.service.resolve_list(ctx.args.list_ref)
    resolved = [ctx.service.resolve_task(target["id"], ref) for ref in ctx.args.task]
    titles = ", ".join(repr(t.get("title")) for t in resolved)
    if not ctx.confirm(f"delete {len(resolved)} task(s): {titles}?"):
        ctx.note("aborted")
        return 0
    for task in resolved:
        ctx.service.delete_task(target["id"], task["id"])
        ctx.note(f"deleted {short_id(task['id'])}  {task.get('title')}")
    ctx.emit([{"deleted": t["id"]} for t in resolved], "")
    return 0


def cmd_task_move(ctx: Context) -> int:
    source = ctx.service.resolve_list(ctx.args.list_ref)
    destination = ctx.service.resolve_list(ctx.args.to)
    if source["id"] == destination["id"]:
        raise UsageError("source and destination lists are the same")
    task = ctx.service.resolve_task(source["id"], ctx.args.task)
    created = ctx.service.move_task(source["id"], task, destination["id"])
    ctx.emit(
        created,
        f"moved {task.get('title')!r} to {destination.get('displayName')!r} "
        f"(new id {short_id(created.get('id'))})",
    )
    return 0


# ---------------------------------------------------------------- checklist


def _task_ctx(ctx: Context) -> tuple[str, dict[str, Any]]:
    target = ctx.service.resolve_list(ctx.args.list_ref)
    task = ctx.service.resolve_task(target["id"], ctx.args.task)
    return target["id"], task


def cmd_checklist_ls(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    items = ctx.service.list_checklist(list_id, task["id"])
    text = "\n".join(
        f"{'[x]' if i.get('isChecked') else '[ ]'} {short_id(i.get('id'), 8)}  {i.get('displayName')}"
        for i in items
    )
    ctx.emit(items, text or "no checklist items")
    return 0


def cmd_checklist_add(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    created = [ctx.service.add_checklist_item(list_id, task["id"], text) for text in ctx.args.item]
    ctx.emit(created, f"added {len(created)} checklist item(s) to {task.get('title')!r}")
    return 0


def _set_checked(ctx: Context, checked: bool) -> int:
    list_id, task = _task_ctx(ctx)
    results = []
    for ref in ctx.args.item:
        item = ctx.service.resolve_checklist_item(list_id, task["id"], ref)
        results.append(
            ctx.service.update_checklist_item(list_id, task["id"], item["id"], {"isChecked": checked})
        )
        ctx.note(f"{'checked' if checked else 'unchecked'} {item.get('displayName')}")
    ctx.emit(results, "")
    return 0


def cmd_checklist_check(ctx: Context) -> int:
    return _set_checked(ctx, True)


def cmd_checklist_uncheck(ctx: Context) -> int:
    return _set_checked(ctx, False)


def cmd_checklist_rm(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    for ref in ctx.args.item:
        item = ctx.service.resolve_checklist_item(list_id, task["id"], ref)
        ctx.service.delete_checklist_item(list_id, task["id"], item["id"])
        ctx.note(f"deleted checklist item {item.get('displayName')!r}")
    return 0


# -------------------------------------------------------------------- links


def cmd_link_ls(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    links = ctx.service.list_links(list_id, task["id"])
    text = "\n".join(
        f"{short_id(item.get('id'), 8)}  {item.get('displayName')}  "
        f"{item.get('webUrl') or item.get('externalId') or ''}".rstrip()
        for item in links
    )
    ctx.emit(links, text or "no linked resources")
    return 0


def cmd_link_add(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    created = ctx.service.add_link(
        list_id,
        task["id"],
        display_name=ctx.args.name or ctx.args.url or ctx.args.external_id or "link",
        application_name=ctx.args.app,
        web_url=ctx.args.url,
        external_id=ctx.args.external_id,
    )
    ctx.emit(created, f"linked {created.get('displayName')!r} to {task.get('title')!r}")
    return 0


def cmd_link_rm(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    links = ctx.service.list_links(list_id, task["id"])
    needle = ctx.args.link.lower()
    matches = [
        item
        for item in links
        if item.get("id") == ctx.args.link or needle in str(item.get("displayName", "")).lower()
    ]
    if not matches:
        raise UsageError(f"no linked resource matches {ctx.args.link!r}")
    for link in matches:
        ctx.service.delete_link(list_id, task["id"], link["id"])
        ctx.note(f"removed link {link.get('displayName')!r}")
    return 0


# -------------------------------------------------------------- attachments


def cmd_attach_ls(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    items = ctx.service.list_attachments(list_id, task["id"])
    text = "\n".join(
        f"{short_id(a.get('id'), 10)}  {a.get('size', 0):>9}  {a.get('name')}" for a in items
    )
    ctx.emit(items, text or "no attachments")
    return 0


def cmd_attach_add(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    results = []
    for path in ctx.args.file:
        results.append(ctx.service.add_attachment(list_id, task["id"], Path(path), name=ctx.args.name))
        ctx.note(f"attached {path}")
    ctx.emit(results, "")
    return 0


def cmd_attach_get(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    name, data = ctx.service.download_attachment(list_id, task["id"], ctx.args.attachment)
    destination = Path(ctx.args.output or name)
    destination.write_bytes(data)
    ctx.emit({"saved": str(destination), "bytes": len(data)}, f"saved {destination} ({len(data)} bytes)")
    return 0


def cmd_attach_rm(ctx: Context) -> int:
    list_id, task = _task_ctx(ctx)
    if not ctx.confirm(f"delete attachment {ctx.args.attachment}?"):
        ctx.note("aborted")
        return 0
    ctx.service.delete_attachment(list_id, task["id"], ctx.args.attachment)
    ctx.note("deleted")
    return 0


# -------------------------------------------------------------------- delta


def cmd_delta(ctx: Context) -> int:
    target = ctx.service.resolve_list(ctx.args.list_ref)
    state = read_json(delta_state_path(), default={}) or {}
    key = target["id"]

    if ctx.args.reset:
        state.pop(key, None)
        write_json_private(delta_state_path(), state)
        ctx.note("delta state reset; the next run returns a full snapshot")
        if not ctx.args.run:
            return 0

    changes, delta_link = ctx.service.delta_tasks(key, delta_link=state.get(key))
    if delta_link:
        state[key] = delta_link
        write_json_private(delta_state_path(), state)

    removed = [c for c in changes if "@removed" in c]
    upserts = [c for c in changes if "@removed" not in c]
    payload = {
        "list": target.get("displayName"),
        "listId": key,
        "changed": len(upserts),
        "removed": len(removed),
        "hadPriorState": key in state and not ctx.args.reset,
        "upserts": upserts,
        "removedIds": [c.get("id") for c in removed],
    }
    text = render_tasks(upserts, color=ctx.color, now=ctx.now) if upserts else "no changes"
    if removed:
        text += f"\n{len(removed)} removed: " + ", ".join(short_id(c.get('id')) for c in removed)
    ctx.emit(payload, text)
    return 0


# ---------------------------------------------------------------------- raw


def cmd_raw(ctx: Context) -> int:
    body = None
    if ctx.args.data:
        raw = ctx.args.data
        if raw == "-":
            raw = sys.stdin.read()
        elif raw.startswith("@"):
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise UsageError(f"--data is not valid JSON: {exc}") from exc

    result = ctx.service.client.request(ctx.args.method, ctx.args.path, json_body=body)
    dump_json(result)
    return 0


# ------------------------------------------------------------------- parser


def _add_list_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-l",
        "--list",
        dest="list_ref",
        metavar="LIST",
        help="list id, name, name fragment, or a well-known name (default, flagged). "
        "Defaults to the built-in Tasks list.",
    )


def _add_task_fields(parser: argparse.ArgumentParser, *, updating: bool) -> None:
    parser.add_argument("--due", metavar="WHEN", help="due date, e.g. tomorrow, friday, 2026-08-20, +3d")
    parser.add_argument(
        "--due-tz",
        metavar="ZONE",
        help="send the due date in this IANA zone instead of midnight UTC "
        "(only needed when the due time of day matters)",
    )
    parser.add_argument("--start", metavar="WHEN", help="start date")
    parser.add_argument("--reminder", metavar="WHEN", help="reminder time, e.g. 'tomorrow 9am'")
    parser.add_argument("--importance", metavar="LEVEL", help="low, normal or high")
    parser.add_argument("--status", metavar="STATUS", help="notStarted, inProgress, completed, waitingOnOthers, deferred")
    parser.add_argument("-n", "--note", metavar="TEXT", help="task body text")
    parser.add_argument("-c", "--category", action="append", metavar="NAME", help="repeatable, or comma-separated")
    parser.add_argument("--recur", metavar="SPEC", help="daily | daily:2 | weekdays | weekly:mon,thu | biweekly | monthly:15 | yearly")
    parser.add_argument("--recur-until", metavar="WHEN", help="stop recurring after this date")
    parser.add_argument("--recur-count", type=int, metavar="N", help="stop after N occurrences")
    if updating:
        parser.add_argument("--title", help="new title")
        parser.add_argument("--clear-due", action="store_true", help="remove the due date")
        parser.add_argument("--clear-start", action="store_true", help="remove the start date")
        parser.add_argument("--clear-reminder", action="store_true", help="remove the reminder")
        parser.add_argument("--clear-recurrence", action="store_true", help="stop the task recurring")
        parser.add_argument("--clear-categories", action="store_true", help="remove all categories")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo",
        description="Manage Microsoft To Do through the Microsoft Graph API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  todo.py auth login --client-id <app-id> --save\n"
            "  todo.py ls --today --all-lists\n"
            "  todo.py add 'Renew passport' --due friday --importance high -c admin\n"
            "  todo.py add 'Water plants' --recur weekly:mon,thu --due tomorrow\n"
            "  todo.py done 'Renew passport'\n"
            "  todo.py ls --json | jq '.[].title'\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ms-todo-skill {__version__}")
    parser.add_argument("--json", action="store_true", help="emit raw Graph JSON")
    parser.add_argument("--jsonl", action="store_true", help="emit one JSON object per line")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="repeat for more logging")
    parser.add_argument("--dry-run", action="store_true", help="print writes instead of sending them")
    parser.add_argument("-y", "--yes", action="store_true", help="skip destructive-action confirmation")
    parser.add_argument("--tz", metavar="ZONE", help="IANA time zone for date parsing and display")
    parser.add_argument("--client-id", metavar="ID", help="Entra application (client) id")
    parser.add_argument("--tenant", metavar="TENANT", help="common, consumers, organizations or a tenant id")
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="per-request timeout")
    parser.add_argument("--retries", type=int, metavar="N", help="attempts per request, including the first")
    parser.add_argument("--no-cache", action="store_true", help="bypass the cached task-list index")
    color = parser.add_mutually_exclusive_group()
    color.add_argument("--color", dest="color", action="store_true", default=None, help="force colour output")
    color.add_argument("--no-color", dest="color", action="store_false", help="disable colour output")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # auth ---------------------------------------------------------------
    auth = sub.add_parser("auth", help="sign in and inspect credentials").add_subparsers(
        dest="subcommand", metavar="<subcommand>"
    )
    login = auth.add_parser("login", help="sign in with the device code flow")
    login.add_argument("--save", action="store_true", help="persist client id / tenant / tz to config.json")
    login.add_argument("--force", action="store_true", help="sign in even if a valid token is cached")
    # These three are global flags, but `auth login --client-id X` is how anyone
    # would naturally type it, so accept them here as well. argparse.SUPPRESS is
    # load-bearing: without it an omitted flag would reset the namespace to None
    # and clobber a value given before the subcommand.
    login.add_argument(
        "--client-id", dest="client_id", metavar="ID", default=argparse.SUPPRESS,
        help="Entra application (client) id",
    )
    login.add_argument(
        "--tenant", dest="tenant", metavar="TENANT", default=argparse.SUPPRESS,
        help="common, consumers, organizations or a tenant id",
    )
    login.add_argument(
        "--tz", dest="tz", metavar="ZONE", default=argparse.SUPPRESS,
        help="IANA time zone to store in the config",
    )
    login.set_defaults(func=cmd_auth_login)
    auth.add_parser("status", help="show the cached identity").set_defaults(func=cmd_auth_status)
    auth.add_parser("logout", help="delete the cached token").set_defaults(func=cmd_auth_logout)
    auth.add_parser("refresh", help="force a token refresh").set_defaults(func=cmd_auth_refresh)

    # lists --------------------------------------------------------------
    lists = sub.add_parser("lists", help="manage task lists").add_subparsers(
        dest="subcommand", metavar="<subcommand>"
    )
    lists.add_parser("ls", help="show every task list").set_defaults(func=cmd_lists_ls)
    get_list = lists.add_parser("get", help="show one task list")
    get_list.add_argument("list_ref", metavar="LIST")
    get_list.set_defaults(func=cmd_lists_get)
    new_list = lists.add_parser("new", help="create a task list")
    new_list.add_argument("name")
    new_list.set_defaults(func=cmd_lists_new)
    rename = lists.add_parser("rename", help="rename a task list")
    rename.add_argument("list_ref", metavar="LIST")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_lists_rename)
    rm_list = lists.add_parser("rm", help="delete a task list and everything in it")
    rm_list.add_argument("list_ref", metavar="LIST")
    rm_list.set_defaults(func=cmd_lists_rm)

    # ls -----------------------------------------------------------------
    ls = sub.add_parser("ls", aliases=["list"], help="list tasks")
    _add_list_flag(ls)
    ls.add_argument("-a", "--all-lists", action="store_true", help="search every list")
    ls.add_argument("--status", metavar="STATUS", help="filter by status")
    ls.add_argument("--include-completed", action="store_true", help="keep completed tasks in the output")
    ls.add_argument("--importance", metavar="LEVEL")
    ls.add_argument("--overdue", action="store_true", help="due before today")
    ls.add_argument("--today", action="store_true", help="due today or earlier")
    ls.add_argument("--week", action="store_true", help="due within seven days")
    ls.add_argument("--due-before", metavar="WHEN")
    ls.add_argument("--due-after", metavar="WHEN")
    ls.add_argument("--no-due", action="store_true", help="only tasks without a due date")
    ls.add_argument("-c", "--category", action="append", metavar="NAME")
    ls.add_argument("-s", "--search", metavar="TEXT", help="substring match on title, body and categories")
    ls.add_argument("--checklist", action="store_true", help="expand checklist items")
    ls.add_argument("--filter", metavar="ODATA", help="raw $filter passed to Graph (support is limited)")
    ls.add_argument("--sort", choices=SORT_FIELDS, default="due")
    ls.add_argument("--reverse", action="store_true")
    ls.add_argument("--limit", type=int, metavar="N")
    ls.set_defaults(func=cmd_tasks_ls)

    # add ----------------------------------------------------------------
    add = sub.add_parser("add", aliases=["new"], help="create a task")
    add.add_argument("title", nargs="+")
    _add_list_flag(add)
    _add_task_fields(add, updating=False)
    add.add_argument("--checklist", action="append", metavar="TEXT", help="add a subtask, repeatable")
    add.add_argument("--link", action="append", metavar="URL", help="attach a linked resource, repeatable")
    add.add_argument("--link-app", default="ms-todo-skill", metavar="NAME", help="applicationName for --link")
    add.add_argument("--attach", action="append", metavar="PATH", help="attach a file, repeatable")
    add.add_argument("--if-not-exists", action="store_true", help="no-op if a task with this exact title exists")
    add.set_defaults(func=cmd_task_add)

    # get / update / done / rm / move -------------------------------------
    get_task = sub.add_parser("get", aliases=["show"], help="show one task in full")
    get_task.add_argument("task", metavar="TASK")
    _add_list_flag(get_task)
    get_task.set_defaults(func=cmd_task_get)

    update = sub.add_parser("update", aliases=["set"], help="change fields on a task")
    update.add_argument("task", metavar="TASK")
    _add_list_flag(update)
    _add_task_fields(update, updating=True)
    update.set_defaults(func=cmd_task_update)

    for name, handler, helptext in (
        ("done", cmd_task_done, "mark tasks completed"),
        ("undone", cmd_task_undone, "reopen tasks"),
        ("start", cmd_task_start, "mark tasks in progress"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("task", nargs="+", metavar="TASK")
        _add_list_flag(cmd)
        cmd.set_defaults(func=handler)

    rm = sub.add_parser("rm", aliases=["delete"], help="delete tasks")
    rm.add_argument("task", nargs="+", metavar="TASK")
    _add_list_flag(rm)
    rm.set_defaults(func=cmd_task_rm)

    move = sub.add_parser("move", help="move a task to another list (copy then delete)")
    move.add_argument("task", metavar="TASK")
    move.add_argument("--to", required=True, metavar="LIST", help="destination list")
    _add_list_flag(move)
    move.set_defaults(func=cmd_task_move)

    # checklist -----------------------------------------------------------
    checklist = sub.add_parser("checklist", aliases=["sub"], help="manage subtasks").add_subparsers(
        dest="subcommand", metavar="<subcommand>"
    )
    for name, handler, helptext, takes_items in (
        ("ls", cmd_checklist_ls, "show subtasks", False),
        ("add", cmd_checklist_add, "add subtasks", True),
        ("check", cmd_checklist_check, "tick subtasks off", True),
        ("uncheck", cmd_checklist_uncheck, "untick subtasks", True),
        ("rm", cmd_checklist_rm, "delete subtasks", True),
    ):
        cmd = checklist.add_parser(name, help=helptext)
        cmd.add_argument("task", metavar="TASK")
        if takes_items:
            cmd.add_argument("item", nargs="+", metavar="ITEM")
        _add_list_flag(cmd)
        cmd.set_defaults(func=handler)

    # links ---------------------------------------------------------------
    links = sub.add_parser("link", help="manage linked resources").add_subparsers(
        dest="subcommand", metavar="<subcommand>"
    )
    link_ls = links.add_parser("ls", help="show linked resources")
    link_ls.add_argument("task", metavar="TASK")
    _add_list_flag(link_ls)
    link_ls.set_defaults(func=cmd_link_ls)
    link_add = links.add_parser("add", help="link a task back to a source item")
    link_add.add_argument("task", metavar="TASK")
    link_add.add_argument("--url", help="deep link to the source item")
    link_add.add_argument("--name", help="display name shown in To Do")
    link_add.add_argument("--app", default="ms-todo-skill", help="applicationName")
    link_add.add_argument("--external-id", help="id of the item in your system")
    _add_list_flag(link_add)
    link_add.set_defaults(func=cmd_link_add)
    link_rm = links.add_parser("rm", help="remove a linked resource")
    link_rm.add_argument("task", metavar="TASK")
    link_rm.add_argument("link", metavar="LINK")
    _add_list_flag(link_rm)
    link_rm.set_defaults(func=cmd_link_rm)

    # attachments ----------------------------------------------------------
    attach = sub.add_parser("attach", help="manage file attachments").add_subparsers(
        dest="subcommand", metavar="<subcommand>"
    )
    attach_ls = attach.add_parser("ls", help="show attachments")
    attach_ls.add_argument("task", metavar="TASK")
    _add_list_flag(attach_ls)
    attach_ls.set_defaults(func=cmd_attach_ls)
    attach_add = attach.add_parser("add", help="attach files (upload session above 3 MB)")
    attach_add.add_argument("task", metavar="TASK")
    attach_add.add_argument("file", nargs="+", metavar="PATH")
    attach_add.add_argument("--name", help="override the display name (single file only)")
    _add_list_flag(attach_add)
    attach_add.set_defaults(func=cmd_attach_add)
    attach_get = attach.add_parser("get", help="download an attachment")
    attach_get.add_argument("task", metavar="TASK")
    attach_get.add_argument("attachment", metavar="ATTACHMENT")
    attach_get.add_argument("-o", "--output", metavar="PATH")
    _add_list_flag(attach_get)
    attach_get.set_defaults(func=cmd_attach_get)
    attach_rm = attach.add_parser("rm", help="delete an attachment")
    attach_rm.add_argument("task", metavar="TASK")
    attach_rm.add_argument("attachment", metavar="ATTACHMENT")
    _add_list_flag(attach_rm)
    attach_rm.set_defaults(func=cmd_attach_rm)

    # delta ----------------------------------------------------------------
    delta = sub.add_parser("delta", help="incremental sync for one list")
    _add_list_flag(delta)
    delta.add_argument("--reset", action="store_true", help="forget the stored delta token")
    delta.add_argument("--run", action="store_true", help="with --reset, also fetch a fresh snapshot")
    delta.set_defaults(func=cmd_delta)

    # raw ------------------------------------------------------------------
    raw = sub.add_parser("raw", help="call an arbitrary Graph path with the cached token")
    raw.add_argument("method", metavar="METHOD")
    raw.add_argument("path", metavar="PATH", help="e.g. /me/todo/lists")
    raw.add_argument("--data", metavar="JSON", help="request body: inline JSON, @file, or - for stdin")
    raw.set_defaults(func=cmd_raw)

    return parser


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    _configure_logging(args.verbose)

    try:
        ctx = Context(args)
        handler: Callable[[Context], int] = args.func
        return handler(ctx)
    except MsTodoError as exc:
        print(exc.render(), file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:  # e.g. piping into `head`
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
