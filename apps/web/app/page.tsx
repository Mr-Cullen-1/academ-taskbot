"use client";

import { useEffect, useRef, useState } from "react";
import TaskBoard from "./TaskBoard";

type Person = { id: string; name: string; phone?: string; status: string; roles: string[]; version: number };
type Project = { id: string; name: string; description: string };

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
const api = devLoginEnabled ? process.env.NEXT_PUBLIC_TASKBOT_API_URL || "" : "";
const roles = ["PM", "TEAM_LEAD", "SUBJECT_DEVELOPER", "QA"];

export default function HomePage() {
  const [person, setPerson] = useState<Person | null>(null);
  const [pending, setPending] = useState<Person[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [message, setMessage] = useState("Загрузка…");
  const [devSecret, setDevSecret] = useState("");
  const [devBusy, setDevBusy] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [projectBusy, setProjectBusy] = useState(false);
  const [projectFeedback, setProjectFeedback] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const projectAttempt = useRef<{ payload: string; key: string } | null>(null);
  const initialized = useRef(false);

  async function load(): Promise<boolean> {
    const response = await fetch(`${api}/api/v1/users/me`, { credentials: "include" });
    if (!response.ok) { setPerson(null); setProjects([]); setPending([]); return false; }
    const current: Person = await response.json();
    setPerson(current);
    setMessage("");
    const projectResponse = await fetch(`${api}/api/v1/projects`, { credentials: "include" });
    if (projectResponse.ok) setProjects(await projectResponse.json());
    else { setProjects([]); setMessage("Не удалось загрузить проекты. Обновите страницу или войдите снова."); }
    if (current.roles.includes("SUPER_ADMIN")) {
      const pendingResponse = await fetch(`${api}/api/v1/users/pending`, { credentials: "include" });
      if (pendingResponse.ok) setPending(await pendingResponse.json());
    }
    return true;
  }

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    const initialize = async () => {
      const webApp = window.Telegram?.WebApp;
      webApp?.ready();
      webApp?.expand();
      if (!webApp?.initData && !devLoginEnabled) {
        setMessage("Откройте приложение кнопкой в Telegram-боте.");
        return;
      }
      try {
        const session = await fetch(`${api}/api/v1/auth/session/refresh`, {
          method: "POST", credentials: "include",
        });
        if (session.ok) {
          const data = await session.json();
          sessionStorage.setItem("taskbot_csrf", data.csrf_token);
          if (!await load()) setMessage("Не удалось загрузить профиль. Откройте приложение заново.");
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
        const response = await fetch(`${api}/api/v1/auth/webapp`, {
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
        if (!await load()) setMessage("Не удалось загрузить профиль. Откройте приложение заново.");
      } catch { setMessage("Не удалось связаться с API. Проверьте подключение и попробуйте снова."); }
    };
    void initialize();
  }, []);

  async function decide(candidate: Person, status: "ACTIVE" | "REJECTED", role: string) {
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) { setMessage("Для административного действия войдите через Telegram в этой вкладке."); return; }
    const response = await fetch(`${api}/api/v1/users/${candidate.id}/status`, {
      method: "PATCH", credentials: "include",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ status, expected_version: candidate.version, ...(status === "ACTIVE" ? { roles: [role] } : {}) }),
    });
    if (!response.ok) { setMessage("Не удалось обработать заявку. Обновите страницу и проверьте права."); return; }
    await load();
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
      setProjectFeedback({ kind: "success", text: `Проект «${created.name}» создан и доступен для задач.` });
    } catch {
      setProjectFeedback({ kind: "error", text: "Не удалось связаться с API. Проверьте соединение и обновите список проектов перед повтором." });
    } finally { setProjectBusy(false); }
  }

  async function logout() {
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) { setMessage("Для выхода войдите через Telegram в этой вкладке."); return; }
    const response = await fetch(`${api}/api/v1/auth/logout`, {
      method: "POST", credentials: "include", headers: { "X-CSRF-Token": csrf },
    });
    if (!response.ok) { setMessage("Не удалось завершить сессию."); return; }
    sessionStorage.removeItem("taskbot_csrf");
    setPerson(null);
    setProjects([]);
    setPending([]);
    setProjectFeedback(null);
    projectAttempt.current = null;
    setMessage("Вы вышли из системы.");
  }

  return (
    <main>
      <h1>Taskbot</h1>
      {!person ? <>
        <p>{message}</p>
        {devLoginEnabled && <form onSubmit={(event) => void localLogin(event)}>
          <h2>Локальный вход для разработки</h2>
          <label>Секрет из локального TASKBOT_DEV_LOGIN_SECRET
            <input type="password" autoComplete="off" value={devSecret}
              onChange={(event) => setDevSecret(event.target.value)} required />
          </label>
          <button type="submit" disabled={devBusy}>Войти локально</button>
        </form>}
        <p>Для регистрации отправьте боту /start.</p>
      </> : <>
        <p>Здравствуйте, {person.name}. Роли: {person.roles.join(", ")}.</p>
        <button onClick={() => void logout()}>Выйти</button>
        {message && <p role="status">{message}</p>}
        <h2>Доступные проекты</h2>
        {person.roles.includes("SUPER_ADMIN") && <form className="application" onSubmit={(event) => void createProject(event)}>
          <h3>Создать проект</h3>
          <label>Название <input value={projectName} maxLength={160} required
            onChange={(event) => { setProjectName(event.target.value); setProjectFeedback(null); }} /></label>
          <label>Описание <textarea value={projectDescription} maxLength={10000}
            onChange={(event) => { setProjectDescription(event.target.value); setProjectFeedback(null); }} /></label>
          <button type="submit" disabled={projectBusy || !projectName.trim()}>
            {projectBusy ? "Создание…" : "Создать проект"}
          </button>
          {projectFeedback && <p role={projectFeedback.kind === "error" ? "alert" : "status"}>
            {projectFeedback.text}
          </p>}
        </form>}
        {projects.length ? <ul>{projects.map((project) => <li key={project.id}>
          <strong>{project.name}</strong>{project.description && <> — {project.description}</>}
        </li>)}</ul> : <p>Пока нет доступных проектов.</p>}
        {person.roles.includes("SUPER_ADMIN") && <section>
          <h2>Заявки на регистрацию</h2>
          {pending.length ? pending.map((candidate) => <Pending key={candidate.id} person={candidate} decide={decide} />) : <p>Новых заявок нет.</p>}
        </section>}
        <TaskBoard api={api} person={person} projects={projects} />
      </>}
    </main>
  );
}

function Pending({ person, decide }: { person: Person; decide: (person: Person, status: "ACTIVE" | "REJECTED", role: string) => Promise<void> }) {
  const [role, setRole] = useState("SUBJECT_DEVELOPER");
  return <div className="application">
    <strong>{person.name}</strong>{person.phone && <span> · {person.phone}</span>}
    <label> Роль: <select value={role} onChange={(event) => setRole(event.target.value)}>
      {roles.map((value) => <option key={value} value={value}>{value}</option>)}
    </select></label>
    <button onClick={() => void decide(person, "ACTIVE", role)}>Одобрить</button>
    <button onClick={() => void decide(person, "REJECTED", role)}>Отклонить</button>
  </div>;
}
