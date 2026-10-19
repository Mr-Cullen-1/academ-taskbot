"use client";

import { useEffect, useRef, useState } from "react";

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

async function apiError(response: Response): Promise<Error> {
  let detail: unknown;
  try { detail = (await response.json()).detail; } catch { /* HTTP status is enough */ }
  if (Array.isArray(detail)) return new Error("Проверьте заполненные поля задачи.");
  const messages: Record<string, string> = {
    AUTH_REQUIRED: "Сессия завершилась. Откройте приложение заново из Telegram.",
    SESSION_INVALID: "Сессия завершилась. Откройте приложение заново из Telegram.",
    CSRF_INVALID: "Сессия этой вкладки изменилась. Откройте приложение заново.",
    VERSION_CONFLICT: "Задача уже изменилась. Обновите её перед повтором.",
    INVALID_STATUS_TRANSITION: "Этот переход статуса больше недоступен. Обновите задачу.",
    REASON_REQUIRED: "Укажите причину или замечание.",
  };
  return new Error(typeof detail === "string" ? messages[detail] || detail : `Ошибка API (${response.status}).`);
}

export default function TaskBoard({ api, person, projects, view, taskId, navigate }: {
  api: string; person: Person; projects: Project[]; view: "list" | "new" | "detail";
  taskId?: string; navigate: (path: string) => void;
}) {
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
  const [roleFilter, setRoleFilter] = useState("");
  const [filtersRestored, setFiltersRestored] = useState(false);
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
  const [mutationBusy, setMutationBusy] = useState(false);
  const mutationLock = useRef(false);
  const attempts = useRef(new Map<string, string>());
  const taskRequest = useRef(0);
  const detailRequest = useRef(0);
  const nameFor = (id: string) => id === person.id ? person.name :
    [...members, ...filterMembers, ...createMembers].find((p) => p.id === id)?.name || id;

  async function request(path: string, init?: RequestInit) {
    let response: Response;
    try {
      response = await fetch(`${api}/api/v1${path}`, { credentials: "include", ...init });
    } catch {
      throw new Error("Не удалось связаться с сервером. Проверьте подключение и попробуйте снова.");
    }
    if (!response.ok) throw await apiError(response);
    return response.json();
  }
  async function mutate(path: string, method: string, body: object) {
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) throw new Error("Войдите через Telegram в этой вкладке для изменения задач.");
    const payload = JSON.stringify(body);
    const identity = `${method}:${path}:${payload}`;
    const key = attempts.current.get(identity) || crypto.randomUUID();
    attempts.current.set(identity, key);
    let response: Response;
    try {
      response = await fetch(`${api}/api/v1${path}`, { method, credentials: "include", headers: {
        "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": key,
      }, body: payload });
    } catch { throw new Error("Нет связи с API. Проверьте состояние задачи перед повтором."); }
    if (!response.ok) {
      if (response.status < 500) attempts.current.delete(identity);
      throw await apiError(response);
    }
    attempts.current.delete(identity);
    return response.json();
  }
  async function withMutation(work: () => Promise<void>) {
    if (mutationLock.current) return;
    mutationLock.current = true;
    setMutationBusy(true);
    try { await work(); }
    finally { mutationLock.current = false; setMutationBusy(false); }
  }
  async function loadTasks() {
    const requestId = ++taskRequest.current;
    setTasksLoading(true);
    setError("");
    setTasks([]);
    const params = new URLSearchParams();
    if (projectFilter) params.set("project_id", projectFilter);
    if (statusFilter) params.set("status", statusFilter);
    if (priorityFilter) params.set("priority", priorityFilter);
    if (assigneeFilter) params.set("assignee_id", assigneeFilter);
    if (roleFilter) params.set(roleFilter, person.id);
    const rows: Task[] = [];
    let cursor = "";
    try {
      while (true) {
        const page: Task[] = await request(`/tasks?${params.toString()}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
        rows.push(...page);
        if (page.length < 100) break;
        cursor = page[page.length - 1].id;
      }
      if (requestId === taskRequest.current) setTasks(rows);
    } finally { if (requestId === taskRequest.current) setTasksLoading(false); }
  }
  async function selectTask(id: string) {
    const requestId = ++detailRequest.current;
    setDetailLoading(true);
    setError("");
    if (selected?.id !== id) setSelected(null);
    try {
      const [task, history, qaHistory, discussion] = await Promise.all([
      request(`/tasks/${id}`), request(`/tasks/${id}/events`),
      request(`/tasks/${id}/qa-reviews`), request(`/tasks/${id}/comments`),
      ]);
      if (requestId === detailRequest.current) {
        setSelected(task); setEvents(history); setQaReviews(qaHistory); setComments(discussion);
        setTitle(task.title); setDescription(task.description);
        setAssignee(task.assignee_id || ""); setReviewer(task.qa_reviewer_id || "");
        setPriority(task.priority); setType(task.type);
      }
    } finally { if (requestId === detailRequest.current) setDetailLoading(false); }
  }
  useEffect(() => {
    if (!taskId || view !== "detail") return;
    void selectTask(taskId).catch((e) => setError((e as Error).message));
  }, [taskId, view]);
  useEffect(() => { if (!createProject && projects.length) setCreateProject(projects[0].id); }, [projects, createProject]);
  useEffect(() => {
    if (projectFilter && projects.length && !projects.some((project) => project.id === projectFilter)) setProjectFilter("");
  }, [projectFilter, projects]);
  useEffect(() => {
    if (view !== "new") return;
    const projectId = new URLSearchParams(window.location.search).get("project_id");
    if (projectId && projects.some((project) => project.id === projectId)) setCreateProject(projectId);
  }, [view, projects]);
  useEffect(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem("taskbot_task_filters") || "{}");
      if (typeof saved.project === "string") setProjectFilter(saved.project);
      if (typeof saved.status === "string") setStatusFilter(saved.status);
      if (typeof saved.priority === "string") setPriorityFilter(saved.priority);
      if (typeof saved.assignee === "string") setAssigneeFilter(saved.assignee);
      if (typeof saved.role === "string") setRoleFilter(saved.role);
    } catch { /* Ignore stale local filters. */ }
    const direct = new URLSearchParams(window.location.search);
    if (direct.has("project_id")) setProjectFilter(direct.get("project_id") || "");
    if (direct.has("status")) setStatusFilter(direct.get("status") || "");
    if (direct.has("assignee_id")) setAssigneeFilter(direct.get("assignee_id") || "");
    setFiltersRestored(true);
  }, []);
  useEffect(() => {
    if (!filtersRestored) return;
    sessionStorage.setItem("taskbot_task_filters", JSON.stringify({ project: projectFilter, status: statusFilter,
      priority: priorityFilter, assignee: assigneeFilter, role: roleFilter }));
    if (view === "list") void loadTasks().catch((e) => setError(e.message));
  }, [filtersRestored, view, projectFilter, statusFilter, priorityFilter, assigneeFilter, roleFilter]);
  useEffect(() => {
    let cancelled = false;
    setCreateMembers([]);
    void (async () => {
      const rows = createProject ? await request(`/projects/${createProject}/members`) : [];
      if (!cancelled) setCreateMembers(rows);
    })().catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [createProject]);
  useEffect(() => {
    let cancelled = false;
    setMembers([]);
    void (async () => {
      const rows = selected ? await request(`/projects/${selected.project_id}/members`) : [];
      if (!cancelled) setMembers(rows);
    })().catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [selected?.project_id]);
  useEffect(() => {
    let cancelled = false;
    setFilterMembers([]);
    void (async () => {
      const sources = projectFilter ? projects.filter((p) => p.id === projectFilter) : projects;
      const groups: Person[][] = await Promise.all(sources.map((p) => request(`/projects/${p.id}/members`)));
      if (!cancelled) setFilterMembers([...new Map(groups.flat().map((p) => [p.id, p])).values()]);
    })().catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [projectFilter, projects]);

  async function createTask() {
    await withMutation(async () => {
    try {
      const task: Task = await mutate("/tasks", "POST", {
        project_id: createProject, title: createTitle, description: createDescription,
        assignee_id: createAssignee || null, qa_reviewer_id: createReviewer || null,
        priority: createPriority, type: createType,
        due_date: createDue,
      });
      setCreateTitle(""); setCreateDescription(""); setCreateAssignee(""); setCreateReviewer(""); setCreateDue("");
      setError(""); await loadTasks(); navigate(`/tasks/${task.id}`);
    } catch (e) { setError((e as Error).message); }
    });
  }
  async function saveTask() {
    if (!selected) return;
    await withMutation(async () => {
    try {
      await mutate(`/tasks/${selected.id}`, "PATCH", {
        expected_version: selected.version, title, description, priority, type,
        ...(selected.available_actions.includes("assign") ? { assignee_id: assignee || null } : {}),
      });
      setError(""); await selectTask(selected.id); await loadTasks();
    } catch (e) { setError((e as Error).message); }
    });
  }
  async function saveReviewer() {
    if (!selected) return;
    await withMutation(async () => {
    try {
      await mutate(`/tasks/${selected.id}/qa-reviewer`, "PUT", {
        expected_version: selected.version, qa_reviewer_id: reviewer || null,
      });
      setError(""); await selectTask(selected.id); await loadTasks();
    } catch (e) { setError((e as Error).message); }
    });
  }
  async function act(action: string) {
    if (!selected) return;
    if (noteRequired.has(action) && !note.trim()) { setError("Укажите причину или замечание."); return; }
    const confirmed = action === "admin-approve" ? window.confirm("Подтверждаете недоступность автора и исключительную приёмку?") :
      action === "cancel" ? window.confirm("Отменить задачу? Это действие нельзя вернуть.") : true;
    if (!confirmed) return;
    await withMutation(async () => {
    try {
      await mutate(`/tasks/${selected.id}/${action}`, "POST", {
        expected_version: selected.version,
        ...(action === "request-rework" ? { comment: note } : noteRequired.has(action) ? { reason: note } : {}),
        ...(action === "submit-review" && note ? { result: note } : {}),
        ...(action === "admin-approve" ? { author_unavailable_confirmed: true } : {}),
      });
      setNote(""); setError(""); await selectTask(selected.id); await loadTasks();
    } catch (e) { setError((e as Error).message); }
    });
  }
  async function addComment() {
    if (!selected) return;
    await withMutation(async () => {
    try {
      await mutate(`/tasks/${selected.id}/comments`, "POST", { body: comment });
      setComment(""); setError(""); await selectTask(selected.id);
    } catch (e) { setError((e as Error).message); }
    });
  }

  return <section className="tasks">
    {error && <p role="alert">{error}</p>}
    {view === "list" && <>
    <div className="section-heading"><div><p className="eyebrow">Рабочее пространство</p><h1>Задачи</h1></div>
      <button className="primary-button" type="button" onClick={() => navigate("/tasks/new")}>Создать задачу</button></div>
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
      <label>Моё участие <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
        <option value="">Любое</option><option value="created_by">Автор</option>
        <option value="assignee_id">Исполнитель</option><option value="qa_reviewer_id">QA</option>
      </select></label>
    </div>
    {tasksLoading && <p role="status">Загрузка задач…</p>}
    <ul className="item-list">{tasks.map((task) => <li key={task.id}>
      <button className="item-card" onClick={() => navigate(`/tasks/${task.id}`)}>
        <span className="item-card-main"><span className="item-kicker">{projects.find((p) => p.id === task.project_id)?.name || task.task_key} · {task.task_key}</span>
          <strong>{task.title}</strong><span className="muted">Срок: {task.current_due_date} · {task.assignee_id ? nameFor(task.assignee_id) : "Без исполнителя"}</span></span>
        <span className="item-card-meta"><span className={`badge badge-${task.status.toLowerCase()}`}>{task.status}</span>
          <span className={`priority priority-${task.priority.toLowerCase()}`}>{task.priority}</span>{task.overdue && <span className="danger-text">Просрочена</span>}</span>
      </button>
    </li>)}</ul>
    {!tasksLoading && !tasks.length && !error && <div className="empty-state surface"><p>Задач по выбранным фильтрам нет.</p></div>}
    </>}

    {view === "new" && <>
    <div className="section-heading"><div><p className="eyebrow">Новая запись</p><h1>Создать задачу</h1></div></div>
    <form className="form-grid surface" onSubmit={(event) => { event.preventDefault(); void createTask(); }}>
      <label>Проект <select value={createProject} onChange={(e) => { setCreateProject(e.target.value); setCreateAssignee(""); setCreateReviewer(""); }}>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>Название <input value={createTitle} onChange={(e) => setCreateTitle(e.target.value)} /></label>
      <label>Описание <textarea value={createDescription} onChange={(e) => setCreateDescription(e.target.value)} /></label>
      <label>Исполнитель <select value={createAssignee} onChange={(e) => {
        setCreateAssignee(e.target.value);
        if (createReviewer === e.target.value) setCreateReviewer("");
      }}>
        <option value="">Не выбран</option>{createMembers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>QA <select value={createReviewer} onChange={(e) => setCreateReviewer(e.target.value)}>
        <option value="">Не назначен</option>{createMembers.filter((p) => p.roles.includes("QA") && p.id !== createAssignee).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select></label>
      <label>Приоритет <select value={createPriority} onChange={(e) => setCreatePriority(e.target.value)}>{priorities.map((p) => <option key={p}>{p}</option>)}</select></label>
      <label>Тип <select value={createType} onChange={(e) => setCreateType(e.target.value)}>{types.map((p) => <option key={p}>{p}</option>)}</select></label>
      <label>Срок (дата) <input type="date" required value={createDue} onChange={(e) => setCreateDue(e.target.value)} /></label>
      <button className="primary-button" type="submit" disabled={mutationBusy || !createProject || !createTitle.trim() || !createDue}>{mutationBusy ? "Создание…" : "Создать задачу"}</button>
    </form>
    </>}

    {view === "detail" && <button className="back-link" onClick={() => navigate("/tasks")}>← К задачам</button>}
    {view === "detail" && detailLoading && <div className="skeleton tall" role="status" aria-label="Загрузка задачи" />}
    {view === "detail" && selected && selected.id === taskId && <section className="task-detail">
      <p className="eyebrow">{selected.task_key} · {projects.find((p) => p.id === selected.project_id)?.name || "Проект"}</p>
      <h1>{selected.title}</h1>
      <div className="chip-row"><span className={`badge badge-${selected.status.toLowerCase()}`}>{selected.status}</span>
        <span className={`priority priority-${selected.priority.toLowerCase()}`}>{selected.priority}</span>
        {selected.overdue && <span className="badge badge-blocked">Просрочена</span>}</div>
      <div className="detail-summary surface"><p>{selected.description || "Описание не указано."}</p>
        <dl><div><dt>Автор</dt><dd>{nameFor(selected.created_by)}</dd></div>
          <div><dt>Исполнитель</dt><dd>{selected.assignee_id ? nameFor(selected.assignee_id) : "Не назначен"}</dd></div>
          <div><dt>QA</dt><dd>{selected.qa_reviewer_id ? nameFor(selected.qa_reviewer_id) : "Не назначен"}</dd></div>
          <div><dt>Проверка</dt><dd>{selected.qa_review_passed ? "Пройдена" : selected.qa_review_status || "Не начата"}</dd></div>
          <div><dt>Срок</dt><dd>{selected.current_due_date}</dd></div>
          {selected.submitted_at && <div><dt>На проверке с</dt><dd>{new Date(selected.submitted_at).toLocaleString()}</dd></div>}
          {selected.completed_at && <div><dt>Принято</dt><dd>{new Date(selected.completed_at).toLocaleString()}</dd></div>}</dl></div>
      {selected.available_actions.includes("edit") && <div className="form-grid surface">
        <h4>Редактирование</h4>
        <label>Название <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label>Описание <textarea value={description} onChange={(e) => setDescription(e.target.value)} /></label>
        {selected.available_actions.includes("assign") && <label>Исполнитель <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
          <option value="">Не выбран</option>{members.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>}
        <label>Приоритет <select value={priority} onChange={(e) => setPriority(e.target.value)}>{priorities.map((p) => <option key={p}>{p}</option>)}</select></label>
        <label>Тип <select value={type} onChange={(e) => setType(e.target.value)}>{types.map((p) => <option key={p}>{p}</option>)}</select></label>
        <button disabled={mutationBusy} onClick={() => void saveTask()}>Сохранить</button>
      </div>}
      {selected.available_actions.includes("set-qa-reviewer") && <div className="surface detail-section">
        <label>QA-проверяющий <select value={reviewer} onChange={(e) => setReviewer(e.target.value)}>
          <option value="">Не назначен</option>{members.filter((p) => p.roles.includes("QA") && p.id !== selected.assignee_id).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>
        <button disabled={mutationBusy} onClick={() => void saveReviewer()}>Сохранить QA</button>
      </div>}
      {selected.available_actions.some((action) => action in actionLabels) && <div className="surface detail-section"><h2>Действия</h2>
        {selected.available_actions.some((action) => noteRequired.has(action) || action === "submit-review") &&
          <label>Причина, замечание или результат для соответствующего действия
            <textarea value={note} onChange={(e) => setNote(e.target.value)} /></label>}
        <div className="actions">{selected.available_actions.filter((a) => a in actionLabels).map((a) =>
          <button key={a} disabled={mutationBusy} onClick={() => void act(a)}>{actionLabels[a]}</button>)}</div></div>}
      <div className="surface detail-section"><h2>Комментарии</h2>
        {comments.length ? <ul>{comments.map((c) => <li key={c.id}>{c.body} · {nameFor(c.author_user_id)} · {new Date(c.created_at).toLocaleString()}</li>)}</ul> : <p className="muted">Комментариев пока нет.</p>}
        <label>Новый комментарий <textarea value={comment} onChange={(e) => setComment(e.target.value)} /></label>
        <button disabled={mutationBusy || !comment.trim()} onClick={() => void addComment()}>Добавить комментарий</button></div>
      <div className="surface detail-section"><h2>История</h2>
      {events.length ? <ol>{events.map((e) => <li key={e.id}>{new Date(e.occurred_at).toLocaleString()} · {e.type} · {nameFor(e.actor_user_id)}
        {Boolean(e.after_data.reason || e.after_data.comment || e.after_data.result) && <> · {String(e.after_data.reason || e.after_data.comment || e.after_data.result)}</>}
      </li>)}</ol> : <p className="muted">Событий пока нет.</p>}
      <h3>История QA-проверок</h3>
      {qaReviews.length ? <ol>{qaReviews.map((e) => <li key={e.id}>{new Date(e.occurred_at).toLocaleString()} · {String(e.after_data.qa_review_status)} · {nameFor(e.actor_user_id)}</li>)}</ol> : <p className="muted">Проверок пока нет.</p>}</div>
    </section>}
  </section>;
}
