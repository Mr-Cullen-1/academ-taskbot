"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

type Notification = { id: string; body: string; kind: string; state: string; task_id: string | null;
  scheduled_at: string; read_at: string | null };
type Reminder = { id: string; task_id: string | null; remind_at: string; text: string; version: number };
type Metrics = { created: number; completed: number; accepted_by_author: number; admin_approved: number;
  current_overdue: number; awaiting_review_or_acceptance: number; awaiting_assignee_response: number;
  pending_deadline_requests: number; on_time_current: number; on_time_original: number };
type Report = { week_start: string; projects: ({ project_id: string; name: string } & Metrics)[]; totals: Metrics };
type TaskReport = { totals: Metrics; by_project: ({ project_id: string; name: string } & Metrics)[];
  previous_period: { created: number; completed: number } };
type UserReport = { users: { user_id: string; name: string; metrics: Metrics }[] };

async function call<T>(api: string, path: string, method = "GET", body?: object): Promise<T> {
  const csrf = sessionStorage.getItem("taskbot_csrf");
  if (method !== "GET" && !csrf) throw new Error("Откройте Mini App заново через Telegram для изменения данных.");
  const response = await fetch(`${api}/api/v1${path}`, { method, credentials: "include",
    headers: method === "GET" ? undefined : {
      "Content-Type": "application/json", "X-CSRF-Token": csrf || "",
      "Idempotency-Key": crypto.randomUUID(),
    }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))).detail;
    throw new Error(typeof detail === "string" ? detail : `Ошибка API (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function NotificationsPanel({ api }: { api: string }) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [remindAt, setRemindAt] = useState("");
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<Reminder | null>(null);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  async function reload() {
    const [notes, personal] = await Promise.all([
      call<Notification[]>(api, "/notifications"), call<Reminder[]>(api, "/reminders"),
    ]);
    setNotifications(notes); setReminders(personal);
  }
  useEffect(() => { void reload().catch((e) => setError(e.message)); }, [api]);
  async function markRead(id: string) {
    setBusy(true); setError("");
    try { await call(api, `/notifications/${id}/read`, "POST", {}); await reload(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function saveReminder() {
    if (!remindAt || !text.trim()) return;
    setBusy(true); setError(""); setFeedback("");
    try {
      const instant = new Date(`${remindAt}:00+05:00`);
      if (Number.isNaN(instant.getTime())) throw new Error("Укажите корректное время.");
      if (editing) await call(api, `/reminders/${editing.id}`, "PATCH", {
        expected_version: editing.version, remind_at: instant.toISOString(), text: text.trim(),
      });
      else await call(api, "/reminders", "POST", {
        remind_at: instant.toISOString(), text: text.trim(),
      });
      setFeedback(editing ? "Напоминание обновлено." : "Напоминание создано.");
      setEditing(null); setRemindAt(""); setText(""); await reload();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function removeReminder(item: Reminder) {
    setBusy(true); setError("");
    try { await call(api, `/reminders/${item.id}`, "DELETE", { expected_version: item.version });
      setFeedback("Напоминание удалено."); await reload(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section><p className="eyebrow">Личное</p><h1>Уведомления и напоминания</h1>
    {error && <p role="alert">{error}</p>}{feedback && <p role="status">{feedback}</p>}
    <div className="surface detail-section"><h2>Уведомления</h2>
      {notifications.length ? <ul>{notifications.map((item) => <li key={item.id}>
        <p>{item.body}</p><small>{new Date(item.scheduled_at).toLocaleString()} · {item.state}</small>
        {item.task_id && <> · <Link href={`/tasks/${item.task_id}`}>Открыть задачу</Link></>}
        {!item.read_at && <button disabled={busy} onClick={() => void markRead(item.id)}>Отметить прочитанным</button>}
      </li>)}</ul> : <p className="muted">Уведомлений нет.</p>}
    </div>
    <div className="surface detail-section"><h2>Личные напоминания</h2>
      {reminders.length ? <ul>{reminders.map((item) => <li key={item.id}>
        {item.text} · {new Date(item.remind_at).toLocaleString()}
        {item.task_id && <> · <Link href={`/tasks/${item.task_id}`}>Задача</Link></>}
        <div><button disabled={busy} onClick={() => { setEditing(item); setText(item.text);
          const local = new Date(new Date(item.remind_at).getTime() + 5 * 60 * 60000);
          setRemindAt(local.toISOString().slice(0, 16)); }}>Изменить</button>
        <button disabled={busy} onClick={() => void removeReminder(item)}>Удалить</button></div>
      </li>)}</ul> : <p className="muted">Напоминаний нет.</p>}
      <div className="form-grid"><label>Время (Asia/Tashkent) <input type="datetime-local" value={remindAt}
        onChange={(e) => setRemindAt(e.target.value)} /></label>
        <label>Текст <input value={text} maxLength={1000} onChange={(e) => setText(e.target.value)} /></label>
        <button disabled={busy || !remindAt || !text.trim()} onClick={() => void saveReminder()}>
          {editing ? "Сохранить" : "Создать напоминание"}</button>
        {editing && <button onClick={() => { setEditing(null); setRemindAt(""); setText(""); }}>Отмена</button>}
      </div>
    </div>
  </section>;
}

export function ReportsPanel({ api, projects, roles }: {
  api: string; projects: { id: string; name: string }[]; roles: string[];
}) {
  const [weekly, setWeekly] = useState<Report | null>(null);
  const [tasks, setTasks] = useState<TaskReport | null>(null);
  const [users, setUsers] = useState<UserReport | null>(null);
  const [project, setProject] = useState("");
  const [status, setStatus] = useState("");
  const [assignee, setAssignee] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const report = await call<Report>(api, "/reports/weekly").catch(() => null);
      if (!cancelled) setWeekly(report);
      if (roles.some((role) => role === "SUPER_ADMIN" || role === "PM")) {
        const params = new URLSearchParams();
        if (project) params.set("project_id", project);
        if (status) params.set("status", status);
        if (assignee) params.set("assignee_id", assignee);
        const [current, perUser] = await Promise.all([
          call<TaskReport>(api, `/reports/tasks?${params.toString()}`),
          call<UserReport>(api, `/reports/users${project ? `?project_id=${encodeURIComponent(project)}` : ""}`),
        ]);
        if (!cancelled) { setTasks(current); setUsers(perUser); }
      }
    })().catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [api, project, status, assignee, roles]);
  return <section><p className="eyebrow">Аналитика</p><h1>Отчёты</h1>
    {error && <p role="alert">{error}</p>}
    {weekly ? <div className="surface detail-section"><h2>Неделя с {weekly.week_start}</h2>
      <p>Создано: {weekly.totals.created} · Завершено: {weekly.totals.completed} ·
        Просрочено сейчас: {weekly.totals.current_overdue} ·
        Ожидают продления: {weekly.totals.pending_deadline_requests}</p>
      <ul>{weekly.projects.map((item) => <li key={item.project_id}>{item.name}: {item.created} создано, {item.completed} завершено</li>)}</ul>
    </div> : <p className="muted">Недельный отчёт ещё не сформирован.</p>}
    {roles.some((role) => role === "SUPER_ADMIN" || role === "PM") && <div className="surface detail-section">
      <h2>Задачи по проектам</h2><label>Проект <select value={project} onChange={(e) => { setProject(e.target.value); setAssignee(""); }}>
        <option value="">Все доступные</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <label>Статус <select value={status} onChange={(e) => setStatus(e.target.value)}>
        <option value="">Любой</option>{["OPEN", "ACCEPTED", "REJECTED", "IN_PROGRESS", "BLOCKED", "REVIEW", "REWORK", "DONE", "CANCELLED"].map((value) =>
          <option key={value}>{value}</option>)}</select></label>
      <label>Исполнитель <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
        <option value="">Все</option>{users?.users.map((item) => <option key={item.user_id} value={item.user_id}>{item.name}</option>)}
      </select></label>
      {tasks && <><p>Создано: {tasks.totals.created} · Завершено: {tasks.totals.completed} ·
        Принято автором: {tasks.totals.accepted_by_author} · Просрочено сейчас: {tasks.totals.current_overdue}</p>
        <p>Предыдущий период: {tasks.previous_period.created} создано, {tasks.previous_period.completed} завершено.</p>
        <ul>{tasks.by_project.map((item) => <li key={item.project_id}>{item.name}: {item.created} создано, {item.completed} завершено</li>)}</ul>
      </>}
    </div>}
    {roles.some((role) => role === "SUPER_ADMIN" || role === "PM") && users &&
      <div className="surface detail-section"><h2>Показатели участников</h2>
        <p className="muted">Рабочие показатели без рейтинга.</p>
        {users.users.length ? <ul>{users.users.map((item) => <li key={item.user_id}>
          {item.name}: завершено {item.metrics.completed}, просрочено сейчас {item.metrics.current_overdue},
          ожидают проверки {item.metrics.awaiting_review_or_acceptance}
        </li>)}</ul> : <p className="muted">Нет участников в доступных проектах.</p>}
      </div>}
  </section>;
}
