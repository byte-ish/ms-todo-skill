"""To Do domain operations built on top of :class:`~mstodo.graph.GraphClient`.

This is where the API's sharp edges are absorbed: opaque ids get friendly
references, ``$filter`` limitations get compensated for client-side, and the
missing "move task" operation gets an honest copy-and-delete implementation.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import time
import urllib.parse
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .config import list_cache_path, read_json, write_json_private
from .dates import from_graph, parse_timestamp
from .errors import AmbiguousReferenceError, NotFoundError, UsageError
from .graph import GraphClient
from .http import request

log = logging.getLogger("mstodo.service")

LIST_CACHE_TTL_SECONDS = 300

#: Graph rejects inline attachments at or above 3 MB; above this we negotiate an
#: upload session instead.
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024

#: Upload session chunks must be a multiple of 320 KiB.
UPLOAD_CHUNK_SIZE = 320 * 1024 * 10

_WELLKNOWN_ALIASES = {
    "default": "defaultList",
    "defaultlist": "defaultList",
    "tasks": "defaultList",
    "inbox": "defaultList",
    "flagged": "flaggedEmails",
    "flaggedemails": "flaggedEmails",
}


def _q(value: str) -> str:
    """Percent-encode an opaque Graph id for use in a URL path segment."""
    return urllib.parse.quote(value, safe="")


def _tail(value: Any, width: int = 12) -> str:
    """Abbreviate an id to its distinguishing tail — see ``format.short_id``."""
    text = str(value or "")
    return text if len(text) <= width else text[-width:]


class TodoService:
    def __init__(self, client: GraphClient, *, use_cache: bool = True) -> None:
        self.client = client
        self.use_cache = use_cache
        self._lists: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------ lists

    def list_lists(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._lists is not None and not refresh:
            return self._lists

        if self.use_cache and not refresh:
            cached = read_json(list_cache_path(), default=None)
            if cached and time.time() - float(cached.get("fetched_at", 0)) < LIST_CACHE_TTL_SECONDS:
                self._lists = list(cached.get("lists", []))
                return self._lists

        lists = list(self.client.paged("/me/todo/lists"))
        self._lists = lists
        if self.use_cache:
            try:
                write_json_private(list_cache_path(), {"fetched_at": time.time(), "lists": lists})
            except OSError:  # pragma: no cover - cache is best-effort
                log.debug("could not write list cache", exc_info=True)
        return lists

    def resolve_list(self, ref: str | None, *, _retried: bool = False) -> dict[str, Any]:
        """Resolve a list by id, well-known name, display name or substring.

        With no reference at all, this returns the built-in **Tasks** list, which
        is where To Do itself puts anything you add without picking a list.
        """
        lists = self.list_lists(refresh=_retried)

        if ref is None:
            for item in lists:
                if item.get("wellknownListName") == "defaultList":
                    return item
            if lists:
                return lists[0]
            raise NotFoundError(
                "no task lists found for this account",
                status=404,
                hint="the mailbox may not be provisioned for To Do yet",
            )

        needle = ref.strip()
        lowered = needle.lower()

        for item in lists:
            if item.get("id") == needle:
                return item

        wellknown = _WELLKNOWN_ALIASES.get(lowered)
        if wellknown:
            for item in lists:
                if item.get("wellknownListName") == wellknown:
                    return item

        exact = [i for i in lists if str(i.get("displayName", "")).lower() == lowered]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise AmbiguousReferenceError(
                f"{len(exact)} lists are named {ref!r}",
                [f"{i['id']}  {i.get('displayName')}" for i in exact],
            )

        partial = [i for i in lists if lowered in str(i.get("displayName", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise AmbiguousReferenceError(
                f"{ref!r} matches {len(partial)} lists",
                [str(i.get("displayName")) for i in partial],
            )

        # Last resort: an id fragment. Names get first refusal because they are
        # what people type; this exists so the abbreviated id printed by
        # `lists ls` (the tail) can be pasted back in. Outlook list ids share a
        # long common head, so this must be a fragment match, not a prefix one.
        if len(needle) >= 6:
            by_id = [i for i in lists if needle in str(i.get("id", ""))]
            if len(by_id) == 1:
                return by_id[0]
            if len(by_id) > 1:
                raise AmbiguousReferenceError(
                    f"id fragment {ref!r} matches {len(by_id)} lists",
                    [f"{_tail(i.get('id'))}  {i.get('displayName')}" for i in by_id],
                )

        # The cache may simply be stale — refresh once before giving up.
        if not _retried and self.use_cache:
            return self.resolve_list(ref, _retried=True)

        raise NotFoundError(
            f"no task list matches {ref!r}",
            status=404,
            hint="run: todo.py lists ls",
        )

    def create_list(self, display_name: str) -> dict[str, Any]:
        result = self.client.post("/me/todo/lists", {"displayName": display_name})
        self._invalidate_list_cache()
        return result

    def rename_list(self, list_id: str, display_name: str) -> dict[str, Any]:
        result = self.client.patch(f"/me/todo/lists/{_q(list_id)}", {"displayName": display_name})
        self._invalidate_list_cache()
        return result

    def delete_list(self, list_id: str) -> None:
        self.client.delete(f"/me/todo/lists/{_q(list_id)}")
        self._invalidate_list_cache()

    def _invalidate_list_cache(self) -> None:
        self._lists = None
        try:
            list_cache_path().unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ tasks

    def iter_tasks(
        self,
        list_id: str,
        *,
        odata_filter: str | None = None,
        select: Iterable[str] | None = None,
        expand: Iterable[str] | None = None,
        limit: int | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"$top": page_size}
        if odata_filter:
            params["$filter"] = odata_filter
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)
        return self.client.paged(f"/me/todo/lists/{_q(list_id)}/tasks", params=params, limit=limit)

    def get_task(self, list_id: str, task_id: str, *, expand: Iterable[str] | None = None) -> dict[str, Any]:
        params = {"$expand": ",".join(expand)} if expand else None
        return self.client.get(f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}", params=params)

    def resolve_task(self, list_id: str, ref: str) -> dict[str, Any]:
        """Resolve a task by exact id, unique id fragment, or title match.

        Id matching is on any *fragment*, not a prefix: Outlook ids share a long
        mailbox-derived head, so a prefix match would be ambiguous across every
        task in the account. This is what lets the abbreviated id printed by
        ``ls`` — which is the tail — be pasted straight back in.
        """
        needle = ref.strip().strip("…")
        if not needle:
            raise UsageError("empty task reference")

        # Long opaque strings are almost certainly ids — try the cheap path first.
        if len(needle) > 40:
            try:
                return self.get_task(list_id, needle)
            except NotFoundError:
                pass

        candidates = list(self.iter_tasks(list_id))
        lowered = needle.lower()

        for task in candidates:
            if task.get("id") == needle:
                return task

        by_id = [t for t in candidates if needle in str(t.get("id", ""))]
        if len(by_id) == 1:
            return by_id[0]

        exact_title = [t for t in candidates if str(t.get("title", "")).lower() == lowered]
        if len(exact_title) == 1:
            return exact_title[0]
        if len(exact_title) > 1:
            raise AmbiguousReferenceError(
                f"{len(exact_title)} tasks are titled {ref!r}",
                [f"{_tail(t.get('id'))}  {t.get('title')}" for t in exact_title],
            )

        partial = [t for t in candidates if lowered in str(t.get("title", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise AmbiguousReferenceError(
                f"{ref!r} matches {len(partial)} tasks",
                [f"{_tail(t.get('id'))}  {t.get('title')}" for t in partial],
            )
        if len(by_id) > 1:
            raise AmbiguousReferenceError(
                f"id fragment {ref!r} matches {len(by_id)} tasks",
                [f"{_tail(t.get('id'), 20)}  {t.get('title')}" for t in by_id],
            )

        raise NotFoundError(f"no task matches {ref!r} in this list", status=404)

    def create_task(self, list_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("title"):
            raise UsageError("a task needs a title")
        return self.client.post(f"/me/todo/lists/{_q(list_id)}/tasks", payload)

    def update_task(self, list_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload:
            raise UsageError("nothing to update", hint="pass at least one field, e.g. --due or --title")
        return self.client.patch(f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}", payload)

    def delete_task(self, list_id: str, task_id: str) -> None:
        self.client.delete(f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}")

    def set_status(self, list_id: str, task_id: str, status: str) -> dict[str, Any]:
        return self.update_task(list_id, task_id, {"status": status})

    def find_by_title(self, list_id: str, title: str) -> dict[str, Any] | None:
        lowered = title.strip().lower()
        for task in self.iter_tasks(list_id):
            if str(task.get("title", "")).strip().lower() == lowered:
                return task
        return None

    def move_task(self, source_list_id: str, task: dict[str, Any], target_list_id: str) -> dict[str, Any]:
        """Copy a task into another list and delete the original.

        Graph has no move operation for todoTask, and ids are list-scoped, so the
        new task gets a new id. Checklist items and linked resources are carried
        across; attachments and open extensions are not.
        """
        task_id = task["id"]
        payload = {
            key: task[key]
            for key in (
                "title", "body", "importance", "status", "categories",
                "dueDateTime", "startDateTime", "completedDateTime",
                "reminderDateTime", "isReminderOn", "recurrence",
            )
            if task.get(key) is not None
        }
        created = self.client.post(f"/me/todo/lists/{_q(target_list_id)}/tasks", payload)
        if self.client.dry_run:
            return created

        new_id = created["id"]
        for item in self.list_checklist(source_list_id, task_id):
            self.add_checklist_item(
                target_list_id, new_id, item.get("displayName", ""), checked=bool(item.get("isChecked"))
            )
        for link in self.list_links(source_list_id, task_id):
            self.add_link(
                target_list_id,
                new_id,
                display_name=link.get("displayName", ""),
                application_name=link.get("applicationName", ""),
                web_url=link.get("webUrl"),
                external_id=link.get("externalId"),
            )
        self.delete_task(source_list_id, task_id)
        return created

    # -------------------------------------------------------- checklist items

    def list_checklist(self, list_id: str, task_id: str) -> list[dict[str, Any]]:
        return list(self.client.paged(f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/checklistItems"))

    def add_checklist_item(
        self, list_id: str, task_id: str, display_name: str, *, checked: bool = False
    ) -> dict[str, Any]:
        return self.client.post(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/checklistItems",
            {"displayName": display_name, "isChecked": checked},
        )

    def update_checklist_item(
        self, list_id: str, task_id: str, item_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self.client.patch(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/checklistItems/{_q(item_id)}", payload
        )

    def delete_checklist_item(self, list_id: str, task_id: str, item_id: str) -> None:
        self.client.delete(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/checklistItems/{_q(item_id)}"
        )

    def resolve_checklist_item(self, list_id: str, task_id: str, ref: str) -> dict[str, Any]:
        items = self.list_checklist(list_id, task_id)
        lowered = ref.strip().lower()
        for item in items:
            if item.get("id") == ref:
                return item
        matches = [i for i in items if lowered in str(i.get("displayName", "")).lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousReferenceError(
                f"{ref!r} matches {len(matches)} checklist items",
                [str(i.get("displayName")) for i in matches],
            )
        raise NotFoundError(f"no checklist item matches {ref!r}", status=404)

    # ------------------------------------------------------- linked resources

    def list_links(self, list_id: str, task_id: str) -> list[dict[str, Any]]:
        return list(self.client.paged(f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/linkedResources"))

    def add_link(
        self,
        list_id: str,
        task_id: str,
        *,
        display_name: str,
        application_name: str,
        web_url: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "displayName": display_name,
            "applicationName": application_name,
        }
        if web_url:
            payload["webUrl"] = web_url
        if external_id:
            payload["externalId"] = external_id
        return self.client.post(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/linkedResources", payload
        )

    def delete_link(self, list_id: str, task_id: str, link_id: str) -> None:
        self.client.delete(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/linkedResources/{_q(link_id)}"
        )

    # ------------------------------------------------------------ attachments

    def list_attachments(self, list_id: str, task_id: str) -> list[dict[str, Any]]:
        return list(self.client.paged(f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/attachments"))

    def add_attachment(self, list_id: str, task_id: str, path: Path, *, name: str | None = None) -> dict[str, Any]:
        """Attach a file, switching to an upload session above 3 MB."""
        if not path.is_file():
            raise UsageError(f"not a file: {path}")
        data = path.read_bytes()
        display_name = name or path.name
        content_type = _guess_content_type(path)

        if len(data) < INLINE_ATTACHMENT_LIMIT:
            return self.client.post(
                f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/attachments",
                {
                    "@odata.type": "#microsoft.graph.taskFileAttachment",
                    "name": display_name,
                    "contentBytes": base64.b64encode(data).decode("ascii"),
                    "contentType": content_type,
                    "size": len(data),
                },
            )
        return self._upload_large_attachment(list_id, task_id, data, display_name, content_type)

    def _upload_large_attachment(
        self, list_id: str, task_id: str, data: bytes, name: str, content_type: str
    ) -> dict[str, Any]:
        session = self.client.post(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/attachments/createUploadSession",
            {
                "attachmentInfo": {
                    "attachmentType": "file",
                    "name": name,
                    "size": len(data),
                    "contentType": content_type,
                }
            },
        )
        if self.client.dry_run:
            return {"dryRun": True, "uploadSession": session}

        upload_url = session["uploadUrl"]
        total = len(data)
        result: dict[str, Any] = {}
        for start in range(0, total, UPLOAD_CHUNK_SIZE):
            chunk = data[start : start + UPLOAD_CHUNK_SIZE]
            end = start + len(chunk) - 1
            # The upload URL is pre-authenticated: sending a bearer token here is
            # both unnecessary and, on some Graph front ends, rejected.
            resp = request(
                "PUT",
                upload_url,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                    "Content-Type": "application/octet-stream",
                },
                body=chunk,
                timeout=max(self.client.config.timeout, 120),
                policy=self.client.policy,
            )
            if resp.status >= 400:
                raise UsageError(
                    f"attachment upload failed at bytes {start}-{end} with HTTP {resp.status}",
                    hint=resp.text[:300] or None,
                )
            payload = resp.json()
            if isinstance(payload, dict):
                result = payload
        return result

    def delete_attachment(self, list_id: str, task_id: str, attachment_id: str) -> None:
        self.client.delete(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/attachments/{_q(attachment_id)}"
        )

    def download_attachment(self, list_id: str, task_id: str, attachment_id: str) -> tuple[str, bytes]:
        payload = self.client.get(
            f"/me/todo/lists/{_q(list_id)}/tasks/{_q(task_id)}/attachments/{_q(attachment_id)}"
        )
        content = payload.get("contentBytes")
        if not content:
            raise UsageError(
                "attachment has no inline content",
                hint="attachments uploaded via an upload session must be fetched from the To Do app",
            )
        return payload.get("name", attachment_id), base64.b64decode(content)

    # ------------------------------------------------------------------ delta

    def delta_tasks(
        self, list_id: str, *, delta_link: str | None = None, page_size: int | None = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        path = delta_link or f"/me/todo/lists/{_q(list_id)}/tasks/delta"
        return self.client.delta(path, page_size=page_size)


def _guess_content_type(path: Path) -> str:
    import mimetypes

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


# ------------------------------------------------------------------ filtering


def task_sort_key(task: dict[str, Any], field: str) -> Any:
    """Sort key that pushes missing values to the end rather than crashing."""
    far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    if field == "due":
        return from_graph(task.get("dueDateTime")) or far_future
    if field == "created":
        return parse_timestamp(task.get("createdDateTime")) or far_future
    if field == "modified":
        return parse_timestamp(task.get("lastModifiedDateTime")) or far_future
    if field == "importance":
        order = {"high": 0, "normal": 1, "low": 2}
        return order.get(str(task.get("importance")), 3)
    if field == "status":
        return str(task.get("status", ""))
    return str(task.get("title", "")).lower()


def filter_tasks(
    tasks: Iterable[dict[str, Any]],
    *,
    status: str | None = None,
    include_completed: bool = False,
    importance: str | None = None,
    due_before: dt.datetime | None = None,
    due_after: dt.datetime | None = None,
    has_due: bool | None = None,
    categories: Iterable[str] | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Apply the filters Graph will not apply for us.

    The To Do endpoint's ``$filter`` support is narrow and inconsistent across
    properties, so every predicate here runs locally against fetched pages.
    """
    wanted_categories = {c.lower() for c in (categories or [])}
    needle = query.lower() if query else None
    out: list[dict[str, Any]] = []

    for task in tasks:
        task_status = str(task.get("status", ""))
        if status and task_status != status:
            continue
        if not status and not include_completed and task_status == "completed":
            continue
        if importance and str(task.get("importance")) != importance:
            continue

        due = from_graph(task.get("dueDateTime"))
        if has_due is True and due is None:
            continue
        if has_due is False and due is not None:
            continue
        if due_before is not None and (due is None or due >= due_before):
            continue
        if due_after is not None and (due is None or due < due_after):
            continue

        if wanted_categories:
            present = {str(c).lower() for c in task.get("categories") or []}
            if not (present & wanted_categories):
                continue

        if needle:
            haystack = " ".join(
                [
                    str(task.get("title", "")),
                    str((task.get("body") or {}).get("content", "")),
                    " ".join(str(c) for c in task.get("categories") or []),
                ]
            ).lower()
            if needle not in haystack:
                continue

        out.append(task)
    return out
