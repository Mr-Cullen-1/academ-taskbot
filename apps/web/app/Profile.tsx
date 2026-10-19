"use client";

import { useEffect, useRef, useState } from "react";
import { pages } from "./apiPages";
import { deadlineCounts, type TaskSummary } from "./taskAnalytics";

type Person = { id: string; name: string; status: string; roles: string[]; version: number };

export default function Profile({ api, person, onChanged, logout, logoutBusy }: {
  api: string; person: Person; onChanged: (person: Person) => void;
  logout: () => Promise<void>; logoutBusy: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(person.name);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [avatar, setAvatar] = useState<string | null>(null);
  const [avatarRevision, setAvatarRevision] = useState(0);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [taskLoading, setTaskLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const attempt = useRef<{ identity: string; key: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetch(`${api}/api/v1/users/me/avatar`, { credentials: "include", cache: "no-store" })
      .then(async (response) => {
        if (response.status === 404) return null;
        if (!response.ok) throw new Error("Не удалось загрузить фотографию.");
        return URL.createObjectURL(await response.blob());
      }).then((url) => { if (cancelled) { if (url) URL.revokeObjectURL(url); } else setAvatar(url); })
      .catch(() => { if (!cancelled) setFeedback({ kind: "error", text: "Не удалось загрузить фотографию." }); });
    return () => { cancelled = true; };
  }, [api, avatarRevision]);
  useEffect(() => () => { if (avatar) URL.revokeObjectURL(avatar); }, [avatar]);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);
  useEffect(() => {
    let cancelled = false;
    void pages<TaskSummary>(api, "/tasks")
      .then((rows) => { if (!cancelled) setTasks(rows.filter((task) => task.assignee_id === person.id)); })
      .catch(() => { if (!cancelled) setFeedback({ kind: "error", text: "Не удалось загрузить показатели задач." }); })
      .finally(() => { if (!cancelled) setTaskLoading(false); });
    return () => { cancelled = true; };
  }, [api, person.id]);

  function chooseFile(next: File | null) {
    setFeedback(null);
    if (next && (!["image/png", "image/jpeg", "image/webp"].includes(next.type) || next.size > 512 * 1024 || next.size === 0)) {
      setFile(null);
      setPreview(null);
      setFeedback({ kind: "error", text: "Выберите PNG, JPEG или WebP размером до 512 КБ." });
      return;
    }
    setFile(next);
    setPreview(next ? URL.createObjectURL(next) : null);
  }

  async function change(path: string, method: "PATCH" | "PUT" | "DELETE", payload: object): Promise<unknown> {
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) throw new Error("Сессия этой вкладки недоступна. Откройте приложение заново.");
    const body = JSON.stringify(payload);
    const identity = `${method}:${path}:${body}`;
    if (attempt.current?.identity !== identity) attempt.current = { identity, key: crypto.randomUUID() };
    const response = await fetch(`${api}/api/v1${path}`, { method, credentials: "include",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Idempotency-Key": attempt.current.key }, body });
    if (!response.ok) {
      if (response.status < 500) attempt.current = null;
      throw new Error(response.status === 409 ? "Профиль изменился. Обновите страницу." :
        response.status === 422 ? "Проверьте имя или формат фотографии." : `Не удалось сохранить профиль (${response.status}).`);
    }
    attempt.current = null;
    return response.json();
  }

  async function saveName() {
    if (busy || name.trim().length < 2) return;
    setBusy(true); setFeedback(null);
    try {
      const updated = await change("/users/me", "PATCH", { name: name.trim(), expected_version: person.version }) as Person;
      onChanged(updated);
      setEditing(false);
      setFeedback({ kind: "success", text: "Имя сохранено." });
    } catch (error) { setFeedback({ kind: "error", text: (error as Error).message }); }
    finally { setBusy(false); }
  }

  async function saveAvatar() {
    if (!file || busy) return;
    setBusy(true); setFeedback(null);
    try {
      const imageBase64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
        reader.onerror = () => reject(new Error("Не удалось прочитать файл."));
        reader.readAsDataURL(file);
      });
      await change("/users/me/avatar", "PUT", { image_base64: imageBase64 });
      chooseFile(null);
      setAvatarRevision((value) => value + 1);
      setFeedback({ kind: "success", text: "Фотография сохранена." });
    } catch (error) { setFeedback({ kind: "error", text: (error as Error).message }); }
    finally { setBusy(false); }
  }

  async function removeAvatar() {
    if (busy || !avatar || !window.confirm("Удалить фотографию профиля?")) return;
    setBusy(true); setFeedback(null);
    try {
      await change("/users/me/avatar", "DELETE", {});
      setAvatar(null);
      chooseFile(null);
      setFeedback({ kind: "success", text: "Фотография удалена." });
    } catch (error) { setFeedback({ kind: "error", text: (error as Error).message }); }
    finally { setBusy(false); }
  }

  const deadlines = deadlineCounts(tasks, new Date());
  return <section><p className="eyebrow">Личный кабинет</p><h1>Профиль</h1>
    <div className="surface profile-card"><span className="profile-avatar">
      {preview || avatar ? <img src={preview || avatar || ""} alt="Фотография профиля" /> : person.name.slice(0, 1).toUpperCase()}
    </span><div><h2>{person.name}</h2><p className="muted">Статус: {person.status}</p></div></div>
    <div className="surface profile-section"><h2>Настройки профиля</h2>
      <div className="profile-settings-row">
      {editing ? <div className="profile-edit"><label>Отображаемое имя<input maxLength={160} value={name}
        onChange={(event) => setName(event.target.value)} /></label><div className="profile-actions">
        <button className="primary-button" type="button" disabled={busy || name.trim().length < 2} onClick={() => void saveName()}>Сохранить имя</button>
        <button className="secondary-button" type="button" disabled={busy} onClick={() => { setName(person.name); setEditing(false); }}>Отмена</button>
      </div></div> : <button className="secondary-button" type="button" onClick={() => { setName(person.name); setEditing(true); }}>Изменить имя</button>}
      <label className="profile-upload"><span>Загрузить фото</span><small>PNG, JPEG или WebP, до 512 КБ</small>
        <input type="file" accept="image/png,image/jpeg,image/webp" aria-label="Загрузить фотографию профиля"
          onChange={(event) => chooseFile(event.target.files?.[0] || null)} /></label>
      </div>
      {file && <p className="muted profile-file-name">Выбрано: {file.name}</p>}
      <div className="profile-actions">{file && <><button className="primary-button" type="button" disabled={busy} onClick={() => void saveAvatar()}>Сохранить фото</button>
        <button className="secondary-button" type="button" disabled={busy} onClick={() => chooseFile(null)}>Отмена</button></>}
        {avatar && !file && <button className="danger-button" type="button" disabled={busy} onClick={() => void removeAvatar()}>Удалить фото</button>}</div>
      {feedback && <p role={feedback.kind === "error" ? "alert" : "status"} className={feedback.kind === "success" ? "success-notice" : ""}>{feedback.text}</p>}
    </div>
    <div className="surface profile-section"><h2>Роли</h2><div className="chip-row">{person.roles.map((role) => <span className="badge" key={role}>{role}</span>)}</div></div>
    <div className="surface profile-section"><h2>Мои задачи и сроки</h2>
      {taskLoading ? <p role="status">Загрузка показателей…</p> : <div className="profile-metrics">
        <span>Назначено: <strong>{tasks.length}</strong></span><span>Завершено: <strong>{tasks.filter((task) => task.status === "DONE").length}</strong></span>
        <span>В срок: <strong>{deadlines.onTime}</strong></span><span>С опозданием: <strong>{deadlines.late}</strong></span>
        <span>Просрочено сейчас: <strong>{deadlines.overdueOpen}</strong></span>
      </div>}
      {!taskLoading && !tasks.length && <p className="muted">Назначенных задач пока нет.</p>}
    </div>
    <div className="profile-future"><h2>Достижения и рейтинг</h2><p className="muted">Появятся после согласования правил начисления баллов.</p></div>
    <button className="secondary-button" disabled={logoutBusy} onClick={() => void logout()}>{logoutBusy ? "Выход…" : "Выйти из системы"}</button>
  </section>;
}
