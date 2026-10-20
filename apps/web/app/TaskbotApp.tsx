"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import TaskBoard from "./TaskBoard";
import { Icon } from "./icons";
import DashboardCharts from "./DashboardCharts";
import { attentionDeadline, dashboardTasks, deadlineCounts, type TaskSummary } from "./taskAnalytics";
import { pages } from "./apiPages";
import Profile from "./Profile";
import { NotificationsPanel, ReportsPanel } from "./MvpPanels";
import PlanningPanel from "./PlanningPanel";

type Person = { id: string; name: string; phone?: string; status: string; roles: string[]; version: number;
  requested_role?: string; requested_project_id?: string | null; requested_project_name?: string | null };
type Project = { id: string; name: string; description: string; owner_user_id: string; version: number };
declare global {
  interface Window {
    Telegram?: { WebApp: {
      initData: string; ready: () => void; expand: () => void;
      BackButton: { show: () => void; hide: () => void; onClick: (callback: () => void) => void;
        offClick: (callback: () => void) => void };
    } };
  }
}

const devLoginEnabled = process.env.NODE_ENV !== "production" &&
  process.env.NEXT_PUBLIC_TASKBOT_DEV_LOGIN_ENABLED === "true";
// The guarded local dev login requires a direct loopback request; Mini App traffic uses the proxy.
const devApi = devLoginEnabled ? process.env.NEXT_PUBLIC_TASKBOT_API_URL || "" : "";
const roles = ["PM", "TEAM_LEAD", "SUBJECT_DEVELOPER", "QA"];
type RouteView = "home" | "projects" | "project-new" | "project-detail" | "tasks" | "task-new" | "task-detail" | "team" | "reviews" | "profile" | "notifications" | "reports";
const RouteContext = createContext<((view: RouteView, id?: string) => ReactNode) | null>(null);

export function TaskbotRoute({ view }: { view: RouteView }) {
  const render = useContext(RouteContext);
  const params = useParams<{ id?: string }>();
  if (!render) throw new Error("Taskbot route must be inside the app shell");
  return <>{render(view, typeof params.id === "string" ? params.id : undefined)}</>;
}

export default function TaskbotApp({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [api, setApi] = useState("");
  const [telegramAvailable, setTelegramAvailable] = useState(false);
  const [person, setPerson] = useState<Person | null>(null);
  const [pending, setPending] = useState<Person[]>([]);
  const [pendingError, setPendingError] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [message, setMessage] = useState("Загрузка…");
  const [devSecret, setDevSecret] = useState("");
  const [devBusy, setDevBusy] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [projectBusy, setProjectBusy] = useState(false);
  const [projectFeedback, setProjectFeedback] = useState<{ kind: "error" | "success"; text: string; projectId?: string } | null>(null);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [usersRefresh, setUsersRefresh] = useState(0);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const decisionLock = useRef(false);
  const projectAttempt = useRef<{ payload: string; key: string } | null>(null);
  const initialized = useRef(false);
  const nested = pathname !== "/" && pathname !== "/projects" && pathname !== "/tasks" &&
    pathname !== "/team" && pathname !== "/reviews" && pathname !== "/profile" &&
    pathname !== "/notifications" && pathname !== "/reports";

  useEffect(() => {
    const webApp = window.Telegram?.WebApp;
    if (!webApp?.initData) return;
    const back = webApp.BackButton;
    if (!back) return;
    if (!nested) { back.hide(); return; }
    const parent = pathname.startsWith("/projects") ? "/projects" : "/tasks";
    const goBack = () => router.push(parent);
    back.show();
    back.onClick(goBack);
    return () => { back.offClick(goBack); back.hide(); };
  }, [nested, pathname, router]);

  async function load(base = api): Promise<boolean> {
    const response = await fetch(`${base}/api/v1/users/me`, { credentials: "include" });
    if (!response.ok) { setPerson(null); setProjects([]); setPending([]); setPendingError(false); return false; }
    const current: Person = await response.json();
    setMessage("");
    try { setProjects(await pages<Project>(base, "/projects")); }
    catch { setProjects([]); setMessage("Не удалось загрузить проекты. Обновите страницу или войдите снова."); }
    if (current.roles.includes("SUPER_ADMIN")) {
      try { setPending(await pages<Person>(base, "/users?status=PENDING")); setPendingError(false); }
      catch { setPending([]); setPendingError(true); setMessage("Не удалось загрузить заявки. Обновите страницу."); }
    } else { setPending([]); setPendingError(false); }
    setPerson(current);
    return true;
  }

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    const initialize = async () => {
      const webApp = window.Telegram?.WebApp;
      const launchedInTelegram = Boolean(webApp?.initData);
      setTelegramAvailable(launchedInTelegram);
      const base = launchedInTelegram ? "" : devApi;
      setApi(base);
      webApp?.ready();
      webApp?.expand();
      if (!webApp?.initData && !devLoginEnabled) {
        setMessage("Откройте приложение кнопкой в Telegram-боте.");
        return;
      }
      try {
        const session = await fetch(`${base}/api/v1/auth/session/refresh`, {
          method: "POST", credentials: "include",
          ...(webApp?.initData ? { headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ init_data: webApp.initData }) } : {}),
        });
        if (session.ok) {
          const data = await session.json();
          sessionStorage.setItem("taskbot_csrf", data.csrf_token);
          if (!await load(base)) setMessage("Не удалось загрузить профиль. Откройте приложение заново.");
          return;
        }
        sessionStorage.removeItem("taskbot_csrf");
        if (session.status !== 401 && session.status !== 403) {
          setMessage("Не удалось восстановить сессию. Попробуйте открыть приложение снова.");
          return;
        }
        if (!webApp?.initData) {
          setMessage("Для локальной разработки войдите ниже.");
          return;
        }
        const response = await fetch(`${base}/api/v1/auth/webapp`, {
          method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ init_data: webApp.initData }),
        });
        if (!response.ok) {
          const detail = (await response.json().catch(() => ({}))).detail;
          setMessage(detail === "ACCOUNT_PENDING" ? "Заявка ожидает одобрения администратора." :
            detail === "ACCOUNT_BLOCKED" ? "Доступ заблокирован. Обратитесь к администратору." :
            detail === "ACCOUNT_REJECTED" ? "Заявка отклонена. Обратитесь к администратору." :
            detail === "REGISTRATION_REQUIRED" ? "Сначала отправьте /start боту и завершите регистрацию." :
            detail === "WEBAPP_INIT_DATA_EXPIRED" || detail === "WEBAPP_INIT_DATA_REPLAYED" ?
              "Откройте приложение заново из бота." : "Вход через Telegram не удался. Откройте приложение заново из бота.");
          return;
        }
        const data = await response.json();
        sessionStorage.setItem("taskbot_csrf", data.csrf_token);
        if (!await load(base)) setMessage("Не удалось загрузить профиль. Откройте приложение заново.");
      } catch { setMessage("Не удалось связаться с API. Проверьте подключение и попробуйте снова."); }
    };
    void initialize();
  }, []);

  async function decide(candidate: Person, status: "ACTIVE" | "REJECTED", chosenRoles: string[]) {
    if (decisionLock.current) return;
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) { setMessage("Для административного действия войдите через Telegram в этой вкладке."); return; }
    decisionLock.current = true;
    setDecisionBusy(true);
    try {
      const response = await fetch(`${api}/api/v1/users/${candidate.id}/status`, {
        method: "PATCH", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ status, expected_version: candidate.version,
          ...(status === "ACTIVE" ? { roles: chosenRoles, project_id: candidate.requested_project_id ?? null } : {}) }),
      });
      if (!response.ok) { setMessage("Не удалось обработать заявку. Обновите страницу и проверьте права."); return; }
      await load();
      setUsersRefresh((current) => current + 1);
    } catch { setMessage("Не удалось связаться с API. Проверьте подключение и обновите список заявок перед повтором."); }
    finally { decisionLock.current = false; setDecisionBusy(false); }
  }

  async function localLogin(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!devSecret || devBusy) return;
    setDevBusy(true);
    try {
      const response = await fetch(`${api}/api/v1/auth/dev`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secret: devSecret }),
      });
      if (!response.ok) { setMessage("Локальный вход отклонён. Проверьте настройки API и секрет."); return; }
      const data = await response.json();
      sessionStorage.setItem("taskbot_csrf", data.csrf_token);
      await load();
    } catch { setMessage("Не удалось связаться с API."); }
    finally { setDevSecret(""); setDevBusy(false); }
  }

  async function createProject(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (projectBusy || !projectName.trim()) return;
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) {
      setProjectFeedback({ kind: "error", text: "Сессия этой вкладки недоступна. Выйдите и войдите снова." });
      return;
    }
    setProjectBusy(true);
    setProjectFeedback(null);
    const payload = JSON.stringify({ name: projectName.trim(), description: projectDescription.trim() });
    if (projectAttempt.current?.payload !== payload) {
      projectAttempt.current = { payload, key: crypto.randomUUID() };
    }
    try {
      const response = await fetch(`${api}/api/v1/projects`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": projectAttempt.current.key },
        body: payload,
      });
      if (!response.ok) {
        if (response.status < 500) projectAttempt.current = null;
        const explanation = response.status === 401 ? "Сессия завершилась. Войдите снова." :
          response.status === 403 ? "Доступ запрещён или сессия этой вкладки недействительна. Войдите снова." :
          response.status === 422 ? "Проверьте название и описание проекта." :
          "Не удалось создать проект. Повторите попытку.";
        setProjectFeedback({ kind: "error", text: explanation });
        return;
      }
      const created: Project = await response.json();
      projectAttempt.current = null;
      setProjects((current) => current.some((project) => project.id === created.id) ? current : [...current, created]);
      setProjectName("");
      setProjectDescription("");
      setProjectFeedback({ kind: "success", text: `Проект «${created.name}» создан и доступен для задач.`, projectId: created.id });
      router.push(`/projects/${created.id}`);
    } catch {
      setProjectFeedback({ kind: "error", text: "Не удалось связаться с API. Проверьте соединение и обновите список проектов перед повтором." });
    } finally { setProjectBusy(false); }
  }

  async function logout() {
    if (logoutBusy) return;
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) { setMessage("Для выхода войдите через Telegram в этой вкладке."); return; }
    setLogoutBusy(true);
    try {
      const response = await fetch(`${api}/api/v1/auth/logout`, {
        method: "POST", credentials: "include", headers: { "X-CSRF-Token": csrf },
      });
      if (!response.ok) { setMessage("Не удалось завершить сессию."); return; }
      sessionStorage.removeItem("taskbot_csrf");
      sessionStorage.removeItem("taskbot_task_filters");
      setPerson(null);
      setProjects([]);
      setPending([]);
      setPendingError(false);
      setProjectFeedback(null);
      projectAttempt.current = null;
      setMessage("Вы вышли из системы.");
    } catch { setMessage("Нет связи с API. Проверьте подключение перед повтором."); }
    finally { setLogoutBusy(false); }
  }

  function renderPage(view: RouteView, id?: string): ReactNode {
    if (!person) return null;
    switch (view) {
      case "home": return <Dashboard api={api} person={person} projects={projects} pendingCount={pending.length} />;
      case "projects": return <section>
        <div className="section-heading"><div><p className="eyebrow">Рабочее пространство</p><h1>Проекты</h1><p className="muted">{person.roles.some((role) => role === "SUPER_ADMIN" || role === "TEAM_LEAD") ? "Все проекты системы" : "Доступные вам проекты"}</p></div>
          {person.roles.some((role) => role === "SUPER_ADMIN" || role === "PM") && <Link className="primary-button" href="/projects/new">Создать проект</Link>}</div>
        {projects.length ? <ul className="card-grid">{projects.map((project) => <li key={project.id}>
          <Link href={`/projects/${project.id}`} className="project-card surface"><span className="project-icon"><Icon name="folder" /></span>
            <strong>{project.name}</strong><span className="muted">{project.description || "Описание не указано"}</span>
            <span className="card-footer">Открыть проект <Icon name="arrow" /></span></Link></li>)}</ul> :
          <div className="empty-state surface"><Icon name="folder" /><h2>Проектов пока нет</h2><p>Доступные проекты появятся здесь.</p></div>}
      </section>;
      case "project-new": return person.roles.some((role) => role === "SUPER_ADMIN" || role === "PM") ? <section>
        <Link href="/projects" className="back-link">← К проектам</Link><p className="eyebrow">Новый проект</p><h1>Создать проект</h1>
        <form className="application surface" onSubmit={(event) => void createProject(event)}>
          <label>Название <input value={projectName} maxLength={160} required
            onChange={(event) => { setProjectName(event.target.value); setProjectFeedback(null); }} /></label>
          <label>Описание <textarea value={projectDescription} maxLength={10000}
            onChange={(event) => { setProjectDescription(event.target.value); setProjectFeedback(null); }} /></label>
          <button className="primary-button" type="submit" disabled={projectBusy || !projectName.trim()}>
            {projectBusy ? "Создание…" : "Создать проект"}</button>
          {projectFeedback && <p role={projectFeedback.kind === "error" ? "alert" : "status"}>{projectFeedback.text}</p>}
        </form>
      </section> : <div className="empty-state surface"><h1>Доступ запрещён</h1><p>Создание проекта недоступно вашей роли.</p></div>;
      case "project-detail": return id ? <ProjectDetail key={id} api={api} projectId={id} canManage={person.roles.includes("SUPER_ADMIN")}
        canPlan={person.roles.includes("SUPER_ADMIN") || person.roles.includes("PM")} personId={person.id}
        onUpdated={(updated) => setProjects((current) => current.map((project) => project.id === updated.id ? updated : project))} /> : null;
      case "tasks":
      case "task-new":
      case "task-detail": return <TaskBoard api={api} person={person} projects={projects}
        view={view === "task-new" ? "new" : view === "task-detail" ? "detail" : "list"}
        taskId={view === "task-detail" ? id : undefined} navigate={(path) => router.push(path)} />;
      case "team": return <section><div className="section-heading"><div><p className="eyebrow">Люди и доступ</p><h1>{person.roles.includes("SUPER_ADMIN") ? "Пользователи" : "Команда"}</h1></div></div>
        {person.roles.includes("SUPER_ADMIN") ? <>
          <section className="surface"><h2>Заявки на регистрацию</h2>
            {pendingError ? <p role="alert">Не удалось загрузить заявки. Обновите страницу.</p> :
              pending.length ? pending.map((candidate) => <Pending key={candidate.id} person={candidate} decide={decide} busy={decisionBusy} />) :
                <p className="muted">Новых заявок нет.</p>}</section>
          <UserManagement key={usersRefresh} api={api} />
        </> : <TeamDirectory api={api} projects={projects} />}</section>;
      case "reviews": return <Reviews api={api} person={person} projects={projects} />;
      case "profile": return <Profile api={api} person={person} onChanged={setPerson}
        logout={logout} logoutBusy={logoutBusy} />;
      case "notifications": return <NotificationsPanel api={api} />;
      case "reports": return person.roles.some((role) => ["SUPER_ADMIN", "TEAM_LEAD", "PM"].includes(role)) ?
        <ReportsPanel api={api} projects={projects} roles={person.roles} /> :
        <div className="empty-state surface"><h1>Отчёты недоступны</h1></div>;
    }
  }

  return (
    <RouteContext.Provider value={renderPage}><div className="app-shell">
      <header className="app-header"><Link href="/" className="brand"><span className="brand-mark"><Icon name="grid" /></span><span>Taskbot</span></Link>
        {person && <Link className="header-user" href="/profile"><span className="header-user-name">{person.name}</span><span className="avatar">{person.name.slice(0, 1).toUpperCase()}</span></Link>}
      </header>
      <main className="app-content">
      {!person ? <section className="auth-panel surface">
        <div className="auth-symbol"><Icon name="lock" /></div><p className="eyebrow">Telegram Mini App</p><h1>Добро пожаловать в Taskbot</h1>
        <p role="status">{message}</p>
        {devLoginEnabled && !telegramAvailable && <form onSubmit={(event) => void localLogin(event)}>
          <h2>Локальный вход для разработки</h2>
          <label>Секрет из локального TASKBOT_DEV_LOGIN_SECRET
            <input type="password" autoComplete="off" value={devSecret}
              onChange={(event) => setDevSecret(event.target.value)} required />
          </label>
          <button className="primary-button" type="submit" disabled={devBusy}>{devBusy ? "Вход…" : "Войти локально"}</button>
        </form>}
        <p className="muted">Для регистрации отправьте боту /start.</p>
      </section> : <>
        {message && <p role="status" className="notice">{message}</p>}
        {projectFeedback?.kind === "success" && pathname === `/projects/${projectFeedback.projectId}` && <p role="status" className="notice success-notice">{projectFeedback.text}</p>}
        {children}
      </>}
      </main>
      {person && <nav className="bottom-nav" aria-label="Основная навигация">{[
        { href: "/", label: "Главная", icon: "home" },
        { href: "/projects", label: "Проекты", icon: "folder" },
        { href: "/tasks", label: "Задачи", icon: "check" },
        person.roles.includes("QA") && !person.roles.some((role) => role === "SUPER_ADMIN" || role === "TEAM_LEAD" || role === "PM") ? { href: "/reviews", label: "Проверки", icon: "shield" } :
          { href: "/team", label: person.roles.includes("SUPER_ADMIN") ? "Люди" : "Команда", icon: "users" },
        { href: "/profile", label: "Профиль", icon: "user" },
      ].map((item) => <Link key={item.href} href={item.href}
        aria-current={item.href === "/" ? pathname === "/" ? "page" : undefined : pathname.startsWith(item.href) ? "page" : undefined}
        className={`nav-item ${item.href === "/" ? pathname === "/" ? "active" : "" : pathname.startsWith(item.href) ? "active" : ""}`}>
        <Icon name={item.icon} /><span>{item.label}</span></Link>)}</nav>}
    </div></RouteContext.Provider>
  );
}

function Dashboard({ api, person, projects, pendingCount }: { api: string; person: Person; projects: Project[]; pendingCount: number }) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    let cancelled = false;
    const refresh = () => { void pages<TaskSummary>(api, "/tasks")
      .then((rows) => { if (!cancelled) { setTasks(rows); setError(""); setNow(new Date()); } })
      .catch(() => { if (!cancelled) setError("Не удалось загрузить задачи. Обновите страницу."); })
      .finally(() => { if (!cancelled) setLoading(false); }); };
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    refresh();
    document.addEventListener("visibilitychange", onVisible);
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => { cancelled = true; document.removeEventListener("visibilitychange", onVisible); window.clearInterval(timer); };
  }, [api]);
  const visible = dashboardTasks(tasks, person);
  const broadScope = person.roles.some((role) => ["SUPER_ADMIN", "PM", "TEAM_LEAD"].includes(role));
  const globalScope = person.roles.some((role) => ["SUPER_ADMIN", "TEAM_LEAD"].includes(role));
  const active = visible.filter((task) => !["DONE", "CANCELLED"].includes(task.status));
  const assigned = active.filter((task) => task.assignee_id === person.id);
  const review = active.filter((task) => task.status === "REVIEW" &&
    (broadScope || !person.roles.includes("QA") || task.qa_reviewer_id === person.id));
  const overdue = deadlineCounts(visible, now).overdueOpen;
  const urgent = attentionDeadline(tasks, person.id, now);
  const overdueAttention = attentionDeadline(tasks, person.id, now, true);
  const recent = [...visible].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")).slice(0, 4);
  const completionTasks = broadScope ? visible : visible.filter((task) => task.assignee_id === person.id);
  return <section>
    <div className="welcome"><p className="eyebrow">Рабочее пространство</p><h1>Здравствуйте, {person.name}</h1>
      <p className="muted">{person.roles.join(" · ")}</p></div>
    {error && <p role="alert">{error}</p>}
    {!loading && !error && [urgent, overdueAttention].map((item) => item && <div
      key={item.task.id} className={`urgent-card surface ${item.overdue ? "urgent-overdue" : ""}`} role="status">
      <div><p className="eyebrow">{item.overdue ? "Просрочено" : "URGENT · Дедлайн скоро"}</p>
        <h2>{item.task.title}</h2><p>{item.overdue ? "Срок уже прошёл" : (() => {
          const minutes = Math.ceil(item.remainingMs / 60_000);
          return `Осталось ${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
        })()}
          {" · Срок: до конца "}{new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" }).format(new Date(`${item.task.current_due_date}T00:00:00Z`))}</p>
        <p className="deadline-cutoff">Просрочка с {new Intl.DateTimeFormat("ru-RU", {
          day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Tashkent",
        }).format(new Date(item.task.due_cutoff_at!))} (Asia/Tashkent)</p></div>
      <Link className="primary-button" href={`/tasks/${item.task.id}`}>Открыть задачу</Link>
    </div>)}
    {loading ? <div className="stat-grid" role="status" aria-label="Загрузка показателей"><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /></div> : !error && <div className="stat-grid">
      <div className="stat-card surface"><span>Мои активные</span><strong>{assigned.length}</strong><small>Назначены вам</small></div>
      <div className="stat-card surface"><span>На проверке</span><strong>{review.length}</strong><small>{globalScope ? "Во всех проектах" : person.roles.includes("QA") && !broadScope ? "Назначены вам как QA" : broadScope ? "В доступных проектах" : "Ваши задачи"}</small></div>
      <div className="stat-card surface"><span>Просрочены</span><strong>{overdue}</strong><small>{globalScope ? "Во всех проектах" : broadScope ? "В доступных проектах" : "Ваши задачи"}</small></div>
    </div>}
    {!loading && !error && <DashboardCharts tasks={visible} completionTasks={completionTasks} now={now}
      scopeLabel={globalScope ? "Все проекты системы" : broadScope ? "Доступные проекты" : "Вы — основной исполнитель"} />}
    <div className="section-heading compact"><h2>Быстрые действия</h2></div>
    <div className="quick-actions"><Link className="quick-action surface" href="/tasks/new"><Icon name="plus" /><span>Новая задача</span></Link>
      <Link className="quick-action surface" href="/projects"><Icon name="folder" /><span>Проекты · {projects.length}</span></Link>
      <Link className="quick-action surface" href="/notifications"><Icon name="check" /><span>Уведомления</span></Link>
      {person.roles.some((role) => ["SUPER_ADMIN", "TEAM_LEAD", "PM"].includes(role)) &&
        <Link className="quick-action surface" href="/reports"><Icon name="grid" /><span>Отчёты</span></Link>}
      {(person.roles.includes("SUPER_ADMIN") || person.roles.includes("PM")) && <Link className="quick-action surface" href="/projects/new"><Icon name="plus" /><span>Новый проект</span></Link>}
      {person.roles.includes("QA") && <Link className="quick-action surface" href="/reviews"><Icon name="shield" /><span>Мои проверки</span></Link>}
      {person.roles.includes("SUPER_ADMIN") && pendingCount > 0 && <Link className="quick-action surface" href="/team"><Icon name="users" /><span>Заявки · {pendingCount}</span></Link>}</div>
    <div className="section-heading compact"><h2>Последние задачи</h2><Link href="/tasks" className="text-link">Все задачи <Icon name="arrow" /></Link></div>
    {loading ? <div className="skeleton tall" role="status" aria-label="Загрузка задач" /> : error ? null : recent.length ? <ul className="item-list">{recent.map((task) => <li key={task.id}>
      <Link href={`/tasks/${task.id}`} className="item-card surface"><span className="item-card-main"><span className="item-kicker">{projects.find((p) => p.id === task.project_id)?.name || task.task_key} · {task.task_key}</span><strong>{task.title}</strong><span className="muted">Срок: {task.current_due_date}</span></span>
        <span className={`badge badge-${task.status.toLowerCase()}`}>{task.status}</span></Link></li>)}</ul> : <div className="empty-state surface"><Icon name="check" /><p>Задач пока нет.</p></div>}
  </section>;
}

function ProjectDetail({ api, projectId, canManage, canPlan, personId, onUpdated }: {
  api: string; projectId: string; canManage: boolean; canPlan: boolean; personId: string; onUpdated: (project: Project) => void;
}) {
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<Person[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editingMembers, setEditingMembers] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void Promise.all([
      fetch(`${api}/api/v1/projects/${projectId}`, { credentials: "include" }).then((res) => { if (!res.ok) throw new Error("Проект недоступен или не найден."); return res.json() as Promise<Project>; }),
      fetch(`${api}/api/v1/projects/${projectId}/members`, { credentials: "include" }).then((res) => { if (!res.ok) throw new Error("Не удалось загрузить участников."); return res.json() as Promise<Person[]>; }),
      pages<TaskSummary>(api, `/tasks?project_id=${encodeURIComponent(projectId)}`),
    ]).then(([item, people, rows]) => { if (!cancelled) { setProject(item); setMembers(people); setTasks(rows); setError(""); } })
      .catch((e) => { if (!cancelled) setError((e as Error).message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, projectId, refresh]);
  return <section><Link href="/projects" className="back-link">← К проектам</Link>
    {loading && <div className="skeleton tall" role="status" aria-label="Загрузка проекта" />}
    {error && <p role="alert">{error}</p>}
    {project && !loading && <>
      <p className="eyebrow">Проект</p><h1>{project.name}</h1>
      <div className="surface"><p className="muted">{project.description || "Описание не указано."}</p>
        <div className="chip-row"><span className="badge">Задач: {tasks.length}</span><span className="badge badge-done">Завершено: {tasks.filter((task) => task.status === "DONE").length}</span></div></div>
      <div className="section-heading compact"><h2>Задачи проекта</h2><Link className="text-link" href={`/tasks/new?project_id=${project.id}`}>Создать задачу <Icon name="arrow" /></Link></div>
      {tasks.length ? <ul className="item-list">{tasks.map((task) => <li key={task.id}><Link className="item-card surface" href={`/tasks/${task.id}`}>
        <span className="item-card-main"><span className="item-kicker">{task.task_key} · {task.current_due_date}</span><strong>{task.title}</strong></span><span className={`badge badge-${task.status.toLowerCase()}`}>{task.status}</span></Link></li>)}</ul> : <div className="empty-state surface"><p>В проекте пока нет задач.</p></div>}
      <div className="section-heading compact"><h2>Участники</h2>{canManage && <button className="text-link" type="button" onClick={() => setEditingMembers((open) => !open)}>{editingMembers ? "Скрыть" : "Управлять доступом"}</button>}</div>
      {members.length ? <ul className="people-list">{members.map((member) => <li className="person-card surface" key={member.id}>
        <span className="avatar">{member.name.slice(0, 1).toUpperCase()}</span><span><strong>{member.name}</strong><small>{member.roles.join(" · ")}</small></span></li>)}</ul> : <p className="muted">Участников нет.</p>}
      {canManage && editingMembers && <ProjectMembership api={api} project={project} onClose={() => setEditingMembers(false)}
        onUpdated={(updated) => { setProject(updated); onUpdated(updated); setRefresh((current) => current + 1); }} />}
      <PlanningPanel api={api} projectId={projectId} canManage={canPlan && (canManage || members.some((member) => member.id === personId))} members={members} />
    </>}
  </section>;
}

function TeamDirectory({ api, projects }: { api: string; projects: Project[] }) {
  const [people, setPeople] = useState<Person[]>([]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      Promise.all(projects.map((project) => fetch(`${api}/api/v1/projects/${project.id}/members`, { credentials: "include" })
        .then((res) => { if (!res.ok) throw new Error("Не удалось загрузить участников проектов."); return res.json() as Promise<Person[]>; }))),
      pages<TaskSummary>(api, "/tasks"),
    ]).then(([groups, rows]) => { if (!cancelled) {
      setPeople([...new Map(groups.flat().map((person) => [person.id, person])).values()].sort((a, b) => a.name.localeCompare(b.name)));
      setTasks(rows.filter((task) => projects.some((project) => project.id === task.project_id)));
    } })
      .catch((e) => { if (!cancelled) setError((e as Error).message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, projects]);
  return <>{loading && <p role="status">Загрузка участников…</p>}{error && <p role="alert">{error}</p>}
    {!loading && !error && <><div className="stat-grid"><div className="stat-card surface"><span>Проекты</span><strong>{projects.length}</strong></div>
      <div className="stat-card surface"><span>Задачи</span><strong>{tasks.length}</strong></div>
      <div className="stat-card surface"><span>Завершены</span><strong>{tasks.filter((task) => task.status === "DONE").length}</strong></div></div>
      <div className="section-heading compact"><h2>Участники проектов</h2><Link href="/tasks" className="text-link">Доступные задачи <Icon name="arrow" /></Link></div></>}
    {!loading && !error && (people.length ? <ul className="people-list">{people.map((member) => <li className="person-card surface" key={member.id}>
      <span className="avatar">{member.name.slice(0, 1).toUpperCase()}</span><span><strong>{member.name}</strong><small>{member.roles.join(" · ")}</small></span></li>)}</ul> : <div className="empty-state surface"><Icon name="users" /><p>Участников в доступных проектах пока нет.</p></div>)}</>;
}

function Reviews({ api, person, projects }: { api: string; person: Person; projects: Project[] }) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!person.roles.includes("QA")) { setLoading(false); return; }
    let cancelled = false;
    void pages<TaskSummary>(api, `/tasks?qa_reviewer_id=${encodeURIComponent(person.id)}&status=REVIEW`)
      .then((rows) => { if (!cancelled) setTasks(rows.filter((task) => task.qa_review_status !== "DONE")); })
      .catch((e) => { if (!cancelled) setError((e as Error).message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, person.id, person.roles]);
  return <section><p className="eyebrow">Контроль качества</p><h1>Проверки</h1>
    {!person.roles.includes("QA") ? <p role="alert">Проверки доступны назначенному QA.</p> : <>
      <p className="muted">Задачи в статусе REVIEW, назначенные вам на проверку.</p>
      {loading && <p role="status">Загрузка проверок…</p>}{error && <p role="alert">{error}</p>}
      {!loading && !error && (tasks.length ? <ul className="item-list">{tasks.map((task) => <li key={task.id}>
        <Link href={`/tasks/${task.id}`} className="item-card surface"><span className="item-card-main"><span className="item-kicker">{projects.find((project) => project.id === task.project_id)?.name || task.task_key} · {task.task_key}</span>
          <strong>{task.title}</strong><span className="muted">Срок: {task.current_due_date}</span></span><span className="badge badge-review">REVIEW</span></Link></li>)}</ul> :
        <div className="empty-state surface"><Icon name="shield" /><p>Задач на проверке сейчас нет.</p></div>)}</>}
  </section>;
}

function Pending({ person, decide, busy }: { person: Person; decide: (person: Person, status: "ACTIVE" | "REJECTED", roles: string[]) => Promise<void>; busy: boolean }) {
  const [chosen, setChosen] = useState([person.requested_role || "SUBJECT_DEVELOPER"]);
  return <div className="application">
    <strong>{person.name}</strong>{person.phone && <span> · {person.phone}</span>}
    {person.requested_role && <p>Запрошена роль: {person.requested_role}.
      Проект: {person.requested_project_name || "не указан"}.</p>}
    <fieldset><legend>Роли</legend>{roles.map((value) => <label key={value}>
      <input type="checkbox" checked={chosen.includes(value)} disabled={busy}
        onChange={(event) => setChosen((current) => event.target.checked ? [...current, value] : current.filter((role) => role !== value))} />{value}
    </label>)}</fieldset>
    <button disabled={busy || !chosen.length} onClick={() => void decide(person, "ACTIVE", chosen)}>Одобрить</button>
    <button disabled={busy} onClick={() => void decide(person, "REJECTED", chosen)}>Отклонить</button>
  </div>;
}

function ProjectMembership({ api, project, onClose, onUpdated }: {
  api: string; project: Project; onClose: () => void; onUpdated: (project: Project) => void;
}) {
  const [users, setUsers] = useState<Person[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const attempt = useRef<{ payload: string; key: string } | null>(null);
  const lock = useRef(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFeedback("");
    setUsers([]);
    setSelected([]);
    void Promise.all([
      pages<Person>(api, "/users?status=ACTIVE"),
      fetch(`${api}/api/v1/projects/${project.id}/members`, { credentials: "include" }),
    ]).then(async ([active, response]) => {
      if (!response.ok) throw new Error(`Не удалось загрузить участников (${response.status}).`);
      const members: Person[] = await response.json();
      if (!cancelled) { setUsers(active); setSelected(members.map((member) => member.id)); }
    }).catch((error) => { if (!cancelled) setFeedback((error as Error).message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, project.id]);

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (lock.current || loading) return;
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) { setFeedback("Сессия этой вкладки недоступна. Войдите снова."); return; }
    if (!selected.includes(project.owner_user_id)) { setFeedback("Создатель проекта должен остаться участником."); return; }
    lock.current = true;
    setBusy(true);
    setFeedback("");
    const payload = JSON.stringify({ members: [...selected].sort(), expected_version: project.version });
    if (attempt.current?.payload !== payload) attempt.current = { payload, key: crypto.randomUUID() };
    try {
      const response = await fetch(`${api}/api/v1/projects/${project.id}/members`, {
        method: "PUT", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": attempt.current.key },
        body: payload,
      });
      if (!response.ok) {
        if (response.status < 500) attempt.current = null;
        setFeedback(response.status === 409 ? "Состав проекта изменился или у удаляемого участника есть активная задача. Обновите страницу перед повтором." :
          response.status === 422 ? "Проверьте, что все выбранные пользователи активны и создатель проекта включён." :
          `Не удалось сохранить участников (${response.status}).`);
        return;
      }
      const result: { project: Project; members: string[] } = await response.json();
      attempt.current = null;
      setSelected(result.members);
      onUpdated(result.project);
      setFeedback("Состав участников сохранён.");
    } catch { setFeedback("Нет связи с API. Проверьте состав проекта перед повтором."); }
    finally { lock.current = false; setBusy(false); }
  }

  return <section className="application">
    <h3>Участники проекта «{project.name}»</h3>
    <button type="button" onClick={onClose} disabled={busy}>Закрыть</button>
    {loading ? <p role="status">Загрузка участников…</p> : <form onSubmit={(event) => void save(event)}>
      {users.map((user) => <label key={user.id}>
        <input type="checkbox" checked={selected.includes(user.id)} disabled={busy || user.id === project.owner_user_id}
          onChange={(event) => {
            attempt.current = null;
            setFeedback("");
            setSelected((current) => event.target.checked ? [...current, user.id] : current.filter((id) => id !== user.id));
          }} />
        {user.name} ({user.roles.join(", ")}){user.id === project.owner_user_id && " — создатель"}
      </label>)}
      <button type="submit" disabled={busy || !users.length}>{busy ? "Сохранение…" : "Сохранить участников"}</button>
    </form>}
    {feedback && <p role={feedback === "Состав участников сохранён." ? "status" : "alert"}>{feedback}</p>}
  </section>;
}

function UserManagement({ api }: { api: string }) {
  const [users, setUsers] = useState<Person[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setUsers([]);
    void Promise.all([pages<Person>(api, "/users?status=ACTIVE"), pages<Person>(api, "/users?status=BLOCKED")])
      .then(([active, blocked]) => {
        if (!cancelled) { setUsers([...active, ...blocked].sort((a, b) => a.name.localeCompare(b.name))); setError(""); }
      }).catch(() => { if (!cancelled) setError("Не удалось загрузить участников. Повторите запрос."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [api, reload]);
  return <section className="user-management">
    <div className="section-heading compact"><h2>Участники системы</h2>
      <button className="text-link" type="button" onClick={() => { setNotice(""); setReload((current) => current + 1); }} disabled={loading}>Обновить список</button></div>
    {notice && <p className="notice success-notice" role="status">{notice}</p>}
    {loading && <p role="status">Загрузка участников…</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !error && !users.length && <p>Участников нет.</p>}
    {!loading && !error && <div className="managed-user-list">{users.map((user) => <ManagedUser key={user.id} api={api} person={user}
      onChanged={(text) => { setNotice(text); setReload((current) => current + 1); }} />)}</div>}
  </section>;
}

function ManagedUser({ api, person, onChanged }: { api: string; person: Person; onChanged: (text: string) => void }) {
  const [chosen, setChosen] = useState(person.roles.filter((role) => roles.includes(role)));
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const lock = useRef(false);
  const attempt = useRef<{ payload: string; key: string } | null>(null);
  const isAdmin = person.roles.includes("SUPER_ADMIN");

  async function change(kind: "roles" | "status") {
    if (lock.current) return;
    if (kind === "status" && person.status === "ACTIVE" && !window.confirm(`Заблокировать ${person.name}?`)) return;
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) { setFeedback("Сессия этой вкладки недоступна. Войдите снова."); return; }
    const payload = JSON.stringify(kind === "roles" ? { roles: chosen, expected_version: person.version } :
      { status: person.status === "ACTIVE" ? "BLOCKED" : "ACTIVE", expected_version: person.version });
    if (attempt.current?.payload !== payload) attempt.current = { payload, key: crypto.randomUUID() };
    lock.current = true;
    setBusy(true);
    setFeedback("");
    try {
      const response = await fetch(`${api}/api/v1/users/${person.id}/${kind}`, {
        method: kind === "roles" ? "PUT" : "PATCH", credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": attempt.current.key },
        body: payload,
      });
      if (!response.ok) {
        if (response.status < 500) attempt.current = null;
        setFeedback(response.status === 409 ? "Данные пользователя изменились. Обновите список." :
          response.status === 422 ? "Выберите хотя бы одну разрешённую роль." : `Действие отклонено (${response.status}).`);
        return;
      }
      attempt.current = null;
      onChanged(kind === "roles" ? `Роли пользователя ${person.name} сохранены.` :
        `Статус пользователя ${person.name} изменён.`);
    } catch { setFeedback("Нет связи с API. Обновите список перед повтором."); }
    finally { lock.current = false; setBusy(false); }
  }

  return <details className="managed-user surface"><summary><strong>{person.name}</strong><span className="badge">{person.status}</span></summary>
    <div className="managed-user-body">
    {isAdmin ? <p>Super Admin</p> : <>
      <fieldset><legend>Роли</legend>{roles.map((role) => <label key={role}>
        <input type="checkbox" checked={chosen.includes(role)} disabled={busy || person.status !== "ACTIVE"}
          onChange={(event) => { setFeedback(""); setChosen((current) => event.target.checked ? [...current, role] : current.filter((item) => item !== role)); }} />{role}
      </label>)}</fieldset>
      <div className="user-actions"><button className="primary-button" type="button" disabled={busy || person.status !== "ACTIVE" || !chosen.length ||
        [...chosen].sort().join() === [...person.roles].sort().join()} onClick={() => void change("roles")}>{busy ? "Сохранение…" : "Сохранить роли"}</button>
        <button className={person.status === "ACTIVE" ? "danger-button" : "secondary-button"} type="button" disabled={busy} onClick={() => void change("status")}>
          {person.status === "ACTIVE" ? "Заблокировать" : "Разблокировать"}
        </button></div>
    </>}
    {feedback && <p role="alert">{feedback}</p>}
    </div>
  </details>;
}
