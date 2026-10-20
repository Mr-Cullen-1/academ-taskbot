"""Shared Phase 3 task rules used by HTTP and Telegram entry points."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskbot_api.models import (
    OutboxEvent, Project, ProjectMember, Sprint, Task, TaskComment, TaskEvent, TaskKeySequence,
    User, now, utc,
)
from taskbot_api.routes import audit, can_view_project
from taskbot_api.security import role_codes


PRIORITIES = {"URGENT", "HIGH", "MEDIUM", "LOW"}
TYPES = {"DEVELOPMENT", "BUG_FIX", "IMPROVEMENT", "QA_CHECK", "RESEARCH"}
TERMINAL = {"DONE", "CANCELLED"}
CANCELLABLE = {"OPEN", "ACCEPTED", "REJECTED", "IN_PROGRESS", "BLOCKED", "REVIEW", "REWORK"}
OVERDUE_STATUSES = {"OPEN", "ACCEPTED", "IN_PROGRESS", "BLOCKED", "REWORK"}
BUSINESS_ZONE = ZoneInfo("Asia/Tashkent")


def is_overdue(task: Task, *, today: date | None = None) -> bool:
    business_date = today or datetime.now(BUSINESS_ZONE).date()
    return task.status in OVERDUE_STATUSES and task.current_due_date < business_date


def as_json(task: Task) -> dict:
    cutoff = (datetime.combine(task.current_due_date + timedelta(days=1), time.min, BUSINESS_ZONE)
              .astimezone(timezone.utc).isoformat()) if task.current_due_date else None
    return {
        "id": str(task.id), "task_key": task.task_key, "project_id": str(task.project_id),
        "sprint_id": str(task.sprint_id) if task.sprint_id else None,
        "title": task.title, "description": task.description,
        "created_by": str(task.created_by),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        "qa_reviewer_id": str(task.qa_reviewer_id) if task.qa_reviewer_id else None,
        "qa_review_status": task.qa_review_status, "status": task.status,
        "qa_review_passed": task.qa_review_status == "DONE" and task.status in {"REVIEW", "DONE"},
        "priority": task.priority, "type": task.type,
        "original_due_date": task.original_due_date.isoformat() if task.original_due_date else None,
        "current_due_date": task.current_due_date.isoformat() if task.current_due_date else None,
        "due_cutoff_at": cutoff,
        "created_at": utc(task.created_at).isoformat() if task.created_at else None,
        "updated_at": utc(task.updated_at).isoformat() if task.updated_at else None,
        "submitted_at": utc(task.submitted_at).isoformat() if task.submitted_at else None,
        "completed_at": utc(task.completed_at).isoformat() if task.completed_at else None,
        "version": task.version,
    }


def event_json(event: TaskEvent) -> dict:
    return {"id": str(event.id), "task_id": str(event.task_id), "sequence": event.sequence,
            "actor_user_id": str(event.actor_user_id), "type": event.type,
            "before_data": event.before_data, "after_data": event.after_data,
            "approval_mode": event.approval_mode,
            "original_author_user_id": str(event.original_author_user_id) if event.original_author_user_id else None,
            "author_unavailable_confirmed": event.author_unavailable_confirmed,
            "intervention_reason": event.intervention_reason,
            "occurred_at": utc(event.occurred_at).isoformat(), "request_id": event.request_id}


def comment_json(comment: TaskComment) -> dict:
    return {"id": str(comment.id), "task_id": str(comment.task_id),
            "author_user_id": str(comment.author_user_id), "body": comment.body,
            "created_at": utc(comment.created_at).isoformat()}


async def visible_task(db: AsyncSession, actor: User, task_id: UUID,
                       *, lock: bool = False) -> Task:
    task = await db.get(Task, task_id, with_for_update=lock)
    if task is None or not await can_view_project(db, actor, task.project_id):
        raise HTTPException(404, "NOT_FOUND")
    return task


async def eligible_member(db: AsyncSession, project_id: UUID, user_id: UUID,
                          *, qa: bool = False) -> User:
    user = await db.get(User, user_id)
    if user is None or user.status != "ACTIVE" or await db.get(ProjectMember, (project_id, user_id)) is None:
        raise HTTPException(422, "ASSIGNEE_MUST_BE_ACTIVE_PROJECT_MEMBER")
    if qa and "QA" not in await role_codes(db, user_id):
        raise HTTPException(422, "QA_REVIEWER_ROLE_REQUIRED")
    return user


async def add_event(db: AsyncSession, task: Task, actor: User, event_type: str,
                    before: dict, after: dict, request_id: str | None = None,
                    *, approval_mode: str | None = None, reason: str | None = None,
                    confirmed: bool | None = None, occurred_at: datetime | None = None) -> TaskEvent:
    event = TaskEvent(id=uuid4(), task_id=task.id, sequence=task.version, actor_user_id=actor.id,
                      type=event_type, before_data=before, after_data=after,
                      approval_mode=approval_mode,
                      original_author_user_id=task.created_by if approval_mode else None,
                      author_unavailable_confirmed=confirmed, intervention_reason=reason,
                      request_id=request_id, occurred_at=occurred_at or now())
    db.add(event)
    # No ORM relationship tells the unit of work that outbox_events depends on
    # task_events. Persist the parent first, within the same transaction.
    await db.flush()
    db.add(OutboxEvent(aggregate_type="task", aggregate_id=str(task.id),
                       source_event_id=event.id, kind="TASK_EVENT",
                       payload={"task_id": str(task.id), "event_type": event_type},
                       dedupe_key=f"task_event:{event.id}", state="UNPROCESSED",
                       available_at=event.occurred_at, created_at=event.occurred_at))
    return event


async def create_task(db: AsyncSession, actor: User, *, project_id: UUID,
                      title: str, description: str, assignee_id: UUID | None,
                      qa_reviewer_id: UUID | None, priority: str, type: str,
                      due_date: date, sprint_id: UUID | None = None,
                      request_id: str | None = None) -> Task:
    if due_date is None:
        raise HTTPException(422, "DUE_DATE_REQUIRED")
    if await db.get(Project, project_id) is None or not await can_view_project(db, actor, project_id):
        raise HTTPException(404, "NOT_FOUND")
    if sprint_id:
        sprint = await db.get(Sprint, sprint_id)
        if sprint is None or sprint.project_id != project_id:
            raise HTTPException(422, "SPRINT_MUST_SHARE_PROJECT")
    if assignee_id:
        await eligible_member(db, project_id, assignee_id)
    if qa_reviewer_id:
        await eligible_member(db, project_id, qa_reviewer_id, qa=True)
    if qa_reviewer_id is not None and qa_reviewer_id == assignee_id:
        raise HTTPException(422, "QA_MUST_DIFFER_FROM_ASSIGNEE")
    if priority not in PRIORITIES or type not in TYPES or not title.strip():
        raise HTTPException(422, "INVALID_TASK_FIELDS")
    sequence = await db.get(TaskKeySequence, 1, with_for_update=True)
    if sequence is None:
        raise HTTPException(503, "TASK_KEY_SEQUENCE_NOT_INITIALIZED")
    task_key = f"TASK-{sequence.next_number}"
    sequence.next_number += 1
    stamp = now()
    task = Task(task_key=task_key, project_id=project_id, sprint_id=sprint_id,
                title=title.strip(),
                description=description, created_by=actor.id, assignee_id=assignee_id,
                qa_reviewer_id=qa_reviewer_id,
                qa_review_status="PENDING" if qa_reviewer_id else None,
                status="OPEN", priority=priority, type=type,
                original_due_date=due_date, current_due_date=due_date,
                created_at=stamp, updated_at=stamp, version=1)
    db.add(task)
    await db.flush()
    await add_event(db, task, actor, "CREATED", {}, as_json(task), request_id, occurred_at=stamp)
    return task


async def update_task(db: AsyncSession, actor: User, task: Task, *, expected_version: int,
                      changes: dict, request_id: str | None = None) -> Task:
    roles = await role_codes(db, actor.id)
    if "PM" not in roles or await db.get(ProjectMember, (task.project_id, actor.id)) is None:
        raise HTTPException(403, "FORBIDDEN")
    if task.status in TERMINAL:
        raise HTTPException(409, "TASK_TERMINAL")
    if task.version != expected_version:
        raise HTTPException(409, "VERSION_CONFLICT")
    before = as_json(task)
    if "assignee_id" in changes:
        if task.status != "OPEN":
            raise HTTPException(409, "ASSIGNEE_CHANGE_REQUIRES_OPEN")
        assignee_id = changes["assignee_id"]
        if assignee_id:
            await eligible_member(db, task.project_id, assignee_id)
        if assignee_id is not None and assignee_id == task.qa_reviewer_id:
            raise HTTPException(422, "QA_MUST_DIFFER_FROM_ASSIGNEE")
        task.assignee_id = assignee_id
    for field in ("title", "description", "priority", "type"):
        if field in changes:
            value = changes[field]
            if value is None:
                raise HTTPException(422, "INVALID_TASK_FIELDS")
            if field == "title" and not value.strip():
                raise HTTPException(422, "INVALID_TITLE")
            if field == "priority" and value not in PRIORITIES:
                raise HTTPException(422, "INVALID_PRIORITY")
            if field == "type" and value not in TYPES:
                raise HTTPException(422, "INVALID_TYPE")
            setattr(task, field, value.strip() if field == "title" else value)
    if as_json(task) == before:
        return task
    task.version += 1
    task.updated_at = now()
    await add_event(db, task, actor, "UPDATED", before, as_json(task), request_id)
    return task


async def set_qa_reviewer(db: AsyncSession, actor: User, task: Task,
                          reviewer_id: UUID | None, expected_version: int,
                          request_id: str | None = None) -> Task:
    if task.created_by != actor.id:
        raise HTTPException(403, "FORBIDDEN")
    if task.status in TERMINAL:
        raise HTTPException(409, "TASK_TERMINAL")
    if task.version != expected_version:
        raise HTTPException(409, "VERSION_CONFLICT")
    if reviewer_id:
        await eligible_member(db, task.project_id, reviewer_id, qa=True)
    if reviewer_id is not None and reviewer_id == task.assignee_id:
        raise HTTPException(422, "QA_MUST_DIFFER_FROM_ASSIGNEE")
    if reviewer_id == task.qa_reviewer_id:
        return task
    before = as_json(task)
    task.qa_reviewer_id = reviewer_id
    task.qa_review_status = "PENDING" if reviewer_id else None
    task.version += 1
    task.updated_at = now()
    await add_event(db, task, actor, "QA_REVIEWER_CHANGED", before, as_json(task), request_id)
    return task


async def transition(db: AsyncSession, actor: User, task: Task, action: str,
                     expected_version: int, *, reason: str | None = None,
                     comment: str | None = None, result: str | None = None,
                     author_unavailable_confirmed: bool = False,
                     request_id: str | None = None) -> Task:
    if task.version != expected_version:
        raise HTTPException(409, "VERSION_CONFLICT")
    if task.status in TERMINAL:
        raise HTTPException(409, "INVALID_STATUS_TRANSITION")
    is_assignee = task.assignee_id == actor.id
    is_author = task.created_by == actor.id
    roles = await role_codes(db, actor.id)
    is_qa = task.qa_reviewer_id == actor.id and "QA" in roles
    is_pm = "PM" in roles and await db.get(ProjectMember, (task.project_id, actor.id)) is not None
    transitions = {
        "accept": ("OPEN", "ACCEPTED"), "reject": ("OPEN", "REJECTED"),
        "block": ("IN_PROGRESS", "BLOCKED"), "resume": ("BLOCKED", "IN_PROGRESS"),
        "submit-review": ("IN_PROGRESS", "REVIEW"),
        "approve": ("REVIEW", "DONE"), "admin-approve": ("REVIEW", "DONE"),
        "request-rework": ("REVIEW", "REWORK"),
    }
    if action == "start":
        if task.status not in {"ACCEPTED", "REWORK"}:
            raise HTTPException(409, "INVALID_STATUS_TRANSITION")
        target = "IN_PROGRESS"
    elif action == "cancel":
        if task.status not in CANCELLABLE:
            raise HTTPException(409, "INVALID_STATUS_TRANSITION")
        target = "CANCELLED"
    elif action == "qa-review/done":
        if task.status != "REVIEW" or task.qa_review_status != "PENDING":
            raise HTTPException(409, "INVALID_QA_TRANSITION")
        target = "REVIEW"
    elif action in transitions:
        source, target = transitions[action]
        if task.status != source:
            raise HTTPException(409, "INVALID_STATUS_TRANSITION")
    else:
        raise HTTPException(404, "UNKNOWN_ACTION")

    if action in {"accept", "reject", "start", "block", "resume", "submit-review"} and not is_assignee:
        raise HTTPException(403, "FORBIDDEN")
    if action == "approve" and not is_author:
        raise HTTPException(403, "FORBIDDEN")
    if action == "admin-approve" and "SUPER_ADMIN" not in roles:
        raise HTTPException(403, "FORBIDDEN")
    if action == "request-rework" and not (is_author or is_qa):
        raise HTTPException(403, "FORBIDDEN")
    if action == "request-rework" and is_qa and not is_author and task.qa_review_status != "PENDING":
        raise HTTPException(409, "INVALID_QA_TRANSITION")
    if action == "qa-review/done" and not is_qa:
        raise HTTPException(403, "FORBIDDEN")
    if action == "cancel" and not (is_author or is_pm):
        raise HTTPException(403, "FORBIDDEN")
    text = reason if action in {"reject", "block", "cancel", "admin-approve"} else comment
    if action in {"reject", "block", "cancel", "admin-approve", "request-rework"} and not (text or "").strip():
        raise HTTPException(422, "REASON_REQUIRED")
    if action == "admin-approve" and not author_unavailable_confirmed:
        raise HTTPException(422, "AUTHOR_UNAVAILABLE_CONFIRMATION_REQUIRED")

    before = as_json(task)
    stamp = now()
    task.status = target
    task.version += 1
    task.updated_at = stamp
    if action == "submit-review":
        task.submitted_at = stamp
        task.qa_review_status = "PENDING" if task.qa_reviewer_id else None
    if action == "request-rework" and is_qa and task.qa_review_status == "PENDING":
        task.qa_review_status = "REWORK"
    if action == "qa-review/done":
        task.qa_review_status = "DONE"
    if action in {"approve", "admin-approve"}:
        task.completed_at = stamp
    after = as_json(task)
    if text:
        after["reason" if action != "request-rework" else "comment"] = text.strip()
    if result:
        after["result"] = result.strip()
    approval_mode = "AUTHOR" if action == "approve" else "ADMIN_OVERRIDE" if action == "admin-approve" else None
    event = await add_event(db, task, actor, action.upper().replace("-", "_").replace("/", "_"),
                            before, after, request_id, approval_mode=approval_mode,
                            reason=text.strip() if action == "admin-approve" else None,
                            confirmed=True if action == "admin-approve" else None, occurred_at=stamp)
    if action == "request-rework":
        db.add(TaskComment(task_id=task.id, author_user_id=actor.id, body=text.strip(), created_at=stamp))
    if action == "admin-approve":
        await db.flush()
        audit(db, actor, "task.admin_approved", "task", str(task.id),
              {"source_event_id": str(event.id), "original_author_user_id": str(task.created_by),
               "author_unavailable_confirmed": True, "reason": text.strip(), "occurred_at": stamp.isoformat()})
    return task


async def add_comment(db: AsyncSession, actor: User, task: Task, body: str,
                      request_id: str | None = None) -> TaskComment:
    if not body.strip():
        raise HTTPException(422, "COMMENT_REQUIRED")
    stamp = now()
    comment = TaskComment(task_id=task.id, author_user_id=actor.id, body=body.strip(), created_at=stamp)
    db.add(comment)
    await db.flush()
    task.version += 1
    task.updated_at = stamp
    await add_event(db, task, actor, "COMMENTED", {},
                    {"comment_id": str(comment.id), "created_by": str(task.created_by),
                     "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                     "qa_reviewer_id": str(task.qa_reviewer_id) if task.qa_reviewer_id else None},
                    request_id, occurred_at=stamp)
    return comment


async def available_actions(db: AsyncSession, actor: User, task: Task) -> list[str]:
    roles = await role_codes(db, actor.id)
    own = task.created_by == actor.id
    assigned = task.assignee_id == actor.id
    qa = task.qa_reviewer_id == actor.id and "QA" in roles
    pm = "PM" in roles and await db.get(ProjectMember, (task.project_id, actor.id)) is not None
    actions = ["comment"]
    if pm and task.status not in TERMINAL:
        actions.append("edit")
        if task.status == "OPEN":
            actions.append("assign")
    if own and task.status not in TERMINAL:
        actions.append("set-qa-reviewer")
    if assigned:
        actions += {"OPEN": ["accept", "reject"], "ACCEPTED": ["start"],
                    "IN_PROGRESS": ["block", "submit-review"], "BLOCKED": ["resume"],
                    "REWORK": ["start"]}.get(task.status, [])
    if task.status == "REVIEW":
        if own:
            actions += ["approve", "request-rework"]
        elif qa and task.qa_review_status == "PENDING":
            actions.append("request-rework")
        if qa and task.qa_review_status == "PENDING":
            actions.append("qa-review/done")
        if "SUPER_ADMIN" in roles:
            actions.append("admin-approve")
    if task.status in CANCELLABLE and (own or pm):
        actions.append("cancel")
    return sorted(set(actions))
