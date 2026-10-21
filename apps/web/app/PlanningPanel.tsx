"use client";

import { useCallback, useEffect, useState } from "react";

type Member = { id: string; name: string };
type Team = { id: string; name: string; version: number };
type Sprint = { id: string; name: string; starts_on: string; ends_on: string; version: number };

export default function PlanningPanel({ api, projectId, canManage, members }: {
  api: string; projectId: string; canManage: boolean; members: Member[];
}) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamMembers, setTeamMembers] = useState<Record<string, string[]>>({});
  const [sprints, setSprints] = useState<Sprint[]>([]);
  const [teamName, setTeamName] = useState("");
  const [sprintName, setSprintName] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    const [teamResponse, sprintResponse] = await Promise.all([
      fetch(`${api}/api/v1/projects/${projectId}/teams`, { credentials: "include" }),
      fetch(`${api}/api/v1/projects/${projectId}/sprints`, { credentials: "include" }),
    ]);
    if (!teamResponse.ok || !sprintResponse.ok) throw new Error("Не удалось загрузить команды и спринты.");
    const nextTeams: Team[] = await teamResponse.json();
    const nextSprints: Sprint[] = await sprintResponse.json();
    const memberships = await Promise.all(nextTeams.map(async (team) => {
      const response = await fetch(`${api}/api/v1/teams/${team.id}/members`, { credentials: "include" });
      if (!response.ok) throw new Error("Не удалось загрузить состав команды.");
      return [team.id, await response.json()] as const;
    }));
    setTeams(nextTeams); setSprints(nextSprints); setTeamMembers(Object.fromEntries(memberships));
  }, [api, projectId]);

  useEffect(() => { void load().catch((error) => setMessage((error as Error).message)); }, [load]);

  async function mutate(path: string, method: string, body: object) {
    const csrf = sessionStorage.getItem("taskbot_csrf");
    if (!csrf) throw new Error("Сессия завершилась. Откройте приложение снова.");
    const response = await fetch(`${api}/api/v1${path}`, {
      method, credentials: "include", headers: { "Content-Type": "application/json",
        "X-CSRF-Token": csrf, "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(response.status === 409 ?
      "Данные уже изменились. Обновите проект и повторите действие." :
      `Не удалось сохранить (${response.status}). Проверьте права и поля.`);
  }

  async function save(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setMessage("");
    try { await work(); await load(); setMessage("Изменения сохранены."); }
    catch (error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  }

  return <div className="surface detail-section planning-panel"><h2>Команды и спринты</h2>
    {message && <p role="status">{message}</p>}
    <h3>Команды</h3>
    {teams.length ? <ul>{teams.map((team) => <li key={team.id}><strong>{team.name}</strong>
      <p>{(teamMembers[team.id] || []).map((id) => members.find((member) => member.id === id)?.name || id).join(", ") || "Участники не выбраны"}</p>
      {canManage && <fieldset disabled={busy}><legend>Участники команды</legend>
        {members.map((member) => <label key={member.id}><input type="checkbox"
          checked={(teamMembers[team.id] || []).includes(member.id)} onChange={(event) => {
            setTeamMembers((current) => ({ ...current, [team.id]: event.target.checked ?
              [...(current[team.id] || []), member.id] : (current[team.id] || []).filter((id) => id !== member.id) }));
          }} />{member.name}</label>)}
        <button type="button" onClick={() => void save(() => mutate(`/teams/${team.id}/members`, "PUT", {
          members: teamMembers[team.id] || [], expected_version: team.version,
        }))}>Сохранить состав</button>
      </fieldset>}</li>)}</ul> : <p className="muted">Команд пока нет.</p>}
    {canManage && <div><label>Название команды <input value={teamName} onChange={(event) => setTeamName(event.target.value)} /></label>
      <button type="button" disabled={busy || !teamName.trim()} onClick={() => void save(async () => {
        await mutate(`/projects/${projectId}/teams`, "POST", { name: teamName.trim() }); setTeamName("");
      })}>Создать команду</button></div>}
    <h3>Спринты</h3>
    {sprints.length ? <ul>{sprints.map((sprint) => <li key={sprint.id}>
      {sprint.name} · {sprint.starts_on} — {sprint.ends_on}
    </li>)}</ul> : <p className="muted">Спринтов пока нет.</p>}
    {canManage && <div className="form-grid">
      <label>Название <input value={sprintName} onChange={(event) => setSprintName(event.target.value)} /></label>
      <label>Начало <input type="date" value={startsOn} onChange={(event) => setStartsOn(event.target.value)} /></label>
      <label>Окончание <input type="date" value={endsOn} onChange={(event) => setEndsOn(event.target.value)} /></label>
      <button type="button" disabled={busy || !sprintName.trim() || !startsOn || !endsOn || endsOn < startsOn}
        onClick={() => void save(async () => {
          await mutate(`/projects/${projectId}/sprints`, "POST", {
            name: sprintName.trim(), starts_on: startsOn, ends_on: endsOn,
          }); setSprintName(""); setStartsOn(""); setEndsOn("");
        })}>Создать спринт</button>
    </div>}
  </div>;
}
