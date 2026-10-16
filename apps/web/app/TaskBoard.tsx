"use client";

import { useEffect, useState } from "react";

type Person = { id: string; name: string; roles: string[] };
type Project = { id: string; name: string };
type Task = {
  id: string; task_key: string; project_id: string; title: string; description: string;
  created_by: string; assignee_id: string | null; qa_reviewer_id: string | null;
  qa_review_status: string | null; qa_review_passed: boolean; status: string; priority: string; type: string;
  current_due_date: string; overdue: boolean; submitted_at: string | null; completed_at: string | null;
  version: number; available_actions: string[];
};
type Event = { id: string; sequence: number; actor_user_id: string; type: string; after_data: Record<string, unknown>; occurred_at: string };
type Comment = { id: string; author_user_id: string; body: string; created_at: string };

const priorities = ["URGENT", "HIGH", "MEDIUM", "LOW"];
const types = ["DEVELOPMENT", "BUG_FIX", "IMPROVEMENT", "QA_CHECK", "RESEARCH"];
const actionLabels: Record<string, string> = {
  accept: "Принять", reject: "Отклонить назначение", start: "Начать работу",
  block: "Заблокировать", resume: "Возобновить", "submit-review": "Отправить на проверку",
  approve: "Принять результат", "admin-approve": "Исключительная приёмка",
  "request-rework": "Вернуть на доработку", "qa-review/done": "QA пройдено",
  cancel: "Отменить",
};
const noteRequired = new Set(["reject", "block", "cancel", "admin-approve", "request-rework"]);

export default function TaskBoard({ api, person, projects }: { api: string; person: Person; projects: Project[] }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<Task | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [qaReviews, setQaReviews] = useState<Event[]>([]);
  const [comments, setComments] = useState<Comment[]>([]);
  const [members, setMembers] = useState<Person[]>([]);
  const [createMembers, setCreateMembers] = useState<Person[]>([]);
  const [filterMembers, setFilterMembers] = useState<Person[]>([]);
  const [projectFilter, setProjectFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [priorityFilter, setPriorityFilter] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [createProject, setCreateProject] = useState(projects[0]?.id || "");
  const [createTitle, setCreateTitle] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createAssignee, setCreateAssignee] = useState("");
  const [createReviewer, setCreateReviewer] = useState("");
  const [createPriority, setCreatePriority] = useState("MEDIUM");
  const [createType, setCreateType] = useState("DEVELOPMENT");
  const [createDue, setCreateDue] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [assignee, setAssignee] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [priority, setPriority] = useState("MEDIUM");
  const [type, setType] = useState("DEVELOPMENT");
  const [note, setNote] = useState("");
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const [tasksLoading, setTasksLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const nameFor = (id: string) => id === person.id ? person.name : members.find((p) => p.id === id)?.name || id;

  async function request(path: string, init?: RequestInit) {
    let response: Response;
    try {
      response = await fetch(`${api}/api/v1${path}`, { credentials: "include", ...init });
    } catch {
      throw new Error("Не удалось связаться с сервером. Проверьте подключение и попробуйте снова.");
    }
    if (!response.ok) {
      let detail = String(response.status);
      try { detail = (await response.json()).detail || detail; } catch { /* HTTP status is enough */ }
      throw new Error(detail);
    }
    return response.json();
  }
  async function mutate(path: string, method: string, body: object) {
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) throw new Error("Войдите через Telegram в этой вкладке для изменения задач.");
    return request(path, { method, headers: {
      "Content-Type": "application/json", "X-CSRF-Token": csrf,
      "Idempotency-Key": crypto.randomUUID(),
    }, body: JSON.stringify(body) });
  }
  async function loadTasks() {
    setTasksLoading(true);
    const params = new URLSearchParams();
    if (projectFilter) params.set("project_id", projectFilter);
    if (statusFilter) params.set("status", statusFilter);
    if (priorityFilter) params.set("priority", priorityFilter);
    if (assigneeFilter) params.set("assignee_id", assigneeFilter);
    try { setTasks(await request(`/tasks?${params.toString()}`)); }
    finally { setTasksLoading(false); }
  }
  async function selectTask(id: string) {
    setDetailLoading(true);
    try {
      const [task, history, qaHistory, discussion] = await Promise.all([
      request(`/tasks/${id}`), request(`/tasks/${id}/events`),
      request(`/tasks/${id}/qa-reviews`), request(`/tasks/${id}/comments`),
      ]);
      setSelected(task); setEvents(history); setQaReviews(qaHistory); setComments(discussion);
      setTitle(task.title); setDescription(task.description);
      setAssignee(task.assignee_id || ""); setReviewer(task.qa_reviewer_id || "");
      setPriority(task.priority); setType(task.type);
    } finally { setDetailLoading(false); }
  }
  useEffect(() => {
    const back = window.Telegram?.WebApp.BackButton;
    if (!back || !selected) { back?.hide(); return; }
    const close = () => setSelected(null);
    back.show();
    back.onClick(close);
    return () => { back.offClick(close); back.hide(); };
  }, [selected]);
  useEffect(() => { if (!createProject && projects.length) setCreateProject(projects[0].id); }, [projects, createProject]);
  useEffect(() => { void loadTasks().catch((e) => setError(e.message)); }, [projectFilter, statusFilter, priorityFilter, assigneeFilter]);
  useEffect(() => {
    void (async () => setCreateMembers(createProject ? await request(`/projects/${createProject}/members`) : []))()
      .catch((e) => setError(e.message));
  }, [createProject]);
  useEffect(() => {
    void (async () => setMembers(selected ? await request(`/projects/${selected.project_id}/members`) : []))()
      .catch((e) => setError(e.message));
  }, [selected?.project_id]);
  useEffect(() => {
    void (async () => {
      const sources = projectFilter ? projects.filter((p) => p.id === projectFilter) : projects;
      const groups: Person[][] = await Promise.all(sources.map((p) => request(`/projects/${p.id}/members`)));
      setFilterMembers([...new Map(groups.flat().map((p) => [p.id, p])).values()]);
    })().catch((e) => setError(e.message));
  }, [projectFilter, projects]);

  async function createTask() {
    try {
      const task: Task = await mutate("/tasks", "POST", {
        project_id: createProject, title: createTitle, description: createDescription,
        assignee_id: createAssignee || null, qa_reviewer_id: createReviewer || null,
        priority: createPriority, type: createType,
        due_date: createDue,
      });
      setCreateTitle(""); setCreateDescription(""); setCreateAssignee(""); setCreateReviewer(""); setCreateDue("");
      setError(""); await loadTasks(); await selectTask(task.id);
    } catch (e) { setError((e as Error).message); }
  }
  async function saveTask() {
    if (!selected) return;
    try {
      await mutate(`/tasks/${selected.id}`, "PATCH", {
        expected_version: selected.version, title, description, priority, type,
        ...(selected.available_actions.includes("assign") ? { assignee_id: assignee || null } : {}),
      });
      setError(""); await selectTask(selected.id); await loadTasks();
    } catch (e) { setError((e as Error).message); }
  }
  async function saveReviewer() {
    if (!selected) return;
    try {
      await mutate(`/tasks/${selected.id}/qa-reviewer`, "PUT", {
        expected_version: selected.version, qa_reviewer_id: reviewer || null,
      });
      setError(""); await selectTask(selected.id);
    } catch (e) { setError((e as Error).message); }
  }
  async function act(action: string) {
    if (!selected) return;
    if (noteRequired.has(action) && !note.trim()) { setError("Укажите причину или замечание."); return; }
    const confirmed = action !== "admin-approve" || window.confirm("Подтверждаете недоступность автора и исключительную приёмку?");
    if (!confirmed) return;
    try {
      await mutate(`/tasks/${selected.id}/${action}`, "POST", {
        expected_version: selected.version,
        ...(action === "request-rework" ? { comment: note } : noteRequired.has(action) ? { reason: note } : {}),
        ...(action === "submit-review" && note ? { result: note } : {}),
        ...(action === "admin-approve" ? { author_unavailable_confirmed: true } : {}),
      });
      setNote(""); setError(""); await selectTask(selected.id); await loadTasks();
    } catch (e) { setError((e as Error).message); }
  }
  async function addComment() {
    if (!selected) return;
    try {
      await mutate(`/tasks/${selected.id}/comments`, "POST", { body: comment });
      setComment(""); setError(""); await selectTask(selected.id);
    } catch (e) { setError((e as Error).message); }
  }

  return <section className="tasks">
    <h2>Задачи</h2>
    {error && <p role="alert">{error}</p>}
    <div className="filters">
      <label>Проект <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
        <option value="">Все доступные</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>Статус <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
        <option value="">Любой</option>{["OPEN", "ACCEPTED", "REJECTED", "IN_PROGRESS", "BLOCKED", "REVIEW", "REWORK", "DONE", "CANCELLED"].map((s) => <option key={s}>{s}</option>)}
      </select></label>
      <label>Приоритет <select value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)}>
        <option value="">Любой</option>{priorities.map((p) => <option key={p}>{p}</option>)}
      </select></label>
      <label>Исполнитель <select value={assigneeFilter} onChange={(e) => setAssigneeFilter(e.target.value)}>
        <option value="">Любой</option>{filterMembers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
    </div>
    {tasksLoading && <p role="status">Загрузка задач…</p>}
    <ul>{tasks.map((task) => <li key={task.id}>
      <button onClick={() => void selectTask(task.id)}>{task.task_key} · {task.title} · {task.status}{task.overdue && " · просрочена"}</button>
    </li>)}</ul>
    {!tasksLoading && !tasks.length && <p>Задач по выбранным фильтрам нет.</p>}

    <h3>Создать задачу</h3>
    <div className="form-grid">
      <label>Проект <select value={createProject} onChange={(e) => { setCreateProject(e.target.value); setCreateAssignee(""); setCreateReviewer(""); }}>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>Название <input value={createTitle} onChange={(e) => setCreateTitle(e.target.value)} /></label>
      <label>Описание <textarea value={createDescription} onChange={(e) => setCreateDescription(e.target.value)} /></label>
      <label>Исполнитель <select value={createAssignee} onChange={(e) => setCreateAssignee(e.target.value)}>
        <option value="">Не выбран</option>{createMembers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>QA <select value={createReviewer} onChange={(e) => setCreateReviewer(e.target.value)}>
        <option value="">Не назначен</option>{createMembers.filter((p) => p.roles.includes("QA") && p.id !== createAssignee).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>Приоритет <select value={createPriority} onChange={(e) => setCreatePriority(e.target.value)}>{priorities.map((p) => <option key={p}>{p}</option>)}</select></label>
      <label>Тип <select value={createType} onChange={(e) => setCreateType(e.target.value)}>{types.map((p) => <option key={p}>{p}</option>)}</select></label>
      <label>Срок (дата) <input type="date" required value={createDue} onChange={(e) => setCreateDue(e.target.value)} /></label>
      <button disabled={!createProject || !createTitle.trim() || !createDue} onClick={() => void createTask()}>Создать</button>
    </div>

    {detailLoading && <p role="status">Загрузка задачи…</p>}
    {selected && <section className="task-detail">
      <button onClick={() => setSelected(null)}>Назад к списку</button>
      <h3>{selected.task_key}: {selected.title}</h3>
      <p>{selected.description}</p>
      <p>Статус: {selected.status}; QA: {selected.qa_review_passed ? "пройдена" : selected.qa_review_status === "DONE" ? "ожидает повторной проверки" : selected.qa_review_status || "не назначен"}; приоритет: {selected.priority}</p>
      <p>Автор: {nameFor(selected.created_by)}; исполнитель: {selected.assignee_id ? nameFor(selected.assignee_id) : "не назначен"}</p>
      <p>Срок: {selected.current_due_date}{selected.overdue && " · просрочена"}</p>
      {selected.submitted_at && <p>Отправлено на проверку: {new Date(selected.submitted_at).toLocaleString()}</p>}
      {selected.completed_at && <p>Окончательно принято: {new Date(selected.completed_at).toLocaleString()}</p>}
      {selected.available_actions.includes("edit") && <div className="form-grid">
        <h4>Редактирование</h4>
        <label>Название <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label>Описание <textarea value={description} onChange={(e) => setDescription(e.target.value)} /></label>
        {selected.available_actions.includes("assign") && <label>Исполнитель <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
          <option value="">Не выбран</option>{members.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>}
        <label>Приоритет <select value={priority} onChange={(e) => setPriority(e.target.value)}>{priorities.map((p) => <option key={p}>{p}</option>)}</select></label>
        <label>Тип <select value={type} onChange={(e) => setType(e.target.value)}>{types.map((p) => <option key={p}>{p}</option>)}</select></label>
        <button onClick={() => void saveTask()}>Сохранить</button>
      </div>}
      {selected.available_actions.includes("set-qa-reviewer") && <div>
        <label>QA-проверяющий <select value={reviewer} onChange={(e) => setReviewer(e.target.value)}>
          <option value="">Не назначен</option>{members.filter((p) => p.roles.includes("QA") && p.id !== selected.assignee_id).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>
        <button onClick={() => void saveReviewer()}>Сохранить QA</button>
      </div>}
      <h4>Действия</h4>
      <label>Причина, замечание или результат <textarea value={note} onChange={(e) => setNote(e.target.value)} /></label>
      <div className="actions">{selected.available_actions.filter((a) => a in actionLabels).map((a) =>
        <button key={a} onClick={() => void act(a)}>{actionLabels[a]}</button>)}</div>
      <h4>Комментарии</h4>
      <ul>{comments.map((c) => <li key={c.id}>{c.body} · {nameFor(c.author_user_id)} · {new Date(c.created_at).toLocaleString()}</li>)}</ul>
      <label>Новый комментарий <textarea value={comment} onChange={(e) => setComment(e.target.value)} /></label>
      <button disabled={!comment.trim()} onClick={() => void addComment()}>Добавить комментарий</button>
      <h4>История</h4>
      <ol>{events.map((e) => <li key={e.id}>{new Date(e.occurred_at).toLocaleString()} · {e.type} · {nameFor(e.actor_user_id)}
        {Boolean(e.after_data.reason || e.after_data.comment || e.after_data.result) && <> · {String(e.after_data.reason || e.after_data.comment || e.after_data.result)}</>}
      </li>)}</ol>
      <h4>История QA-проверок</h4>
      <ol>{qaReviews.map((e) => <li key={e.id}>{new Date(e.occurred_at).toLocaleString()} · {String(e.after_data.qa_review_status)} · {nameFor(e.actor_user_id)}</li>)}</ol>
    </section>}
  </section>;
}
