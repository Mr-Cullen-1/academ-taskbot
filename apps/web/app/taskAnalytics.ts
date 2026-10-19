export type TaskSummary = {
  id: string; task_key: string; title: string; project_id: string; status: string;
  priority: string; created_by: string; assignee_id: string | null; qa_reviewer_id: string | null;
  current_due_date: string; due_cutoff_at: string | null; completed_at: string | null;
  qa_review_status: string | null; overdue: boolean; created_at: string | null;
};

export const statusLabels: Record<string, string> = {
  OPEN: "Открыта", ACCEPTED: "Принята", REJECTED: "Отклонена",
  IN_PROGRESS: "В работе", BLOCKED: "Заблокирована", REVIEW: "На проверке",
  REWORK: "На доработке", DONE: "Завершена", CANCELLED: "Отменена",
};

const deadlineStatuses = new Set(["OPEN", "ACCEPTED", "IN_PROGRESS", "BLOCKED", "REWORK"]);

export function tashkentDateKey(instant: Date): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Tashkent", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(instant);
  const part = (type: string) => parts.find((item) => item.type === type)?.value || "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function dashboardTasks(tasks: TaskSummary[], user: { id: string; roles: string[] }): TaskSummary[] {
  if (user.roles.some((role) => ["SUPER_ADMIN", "PM", "TEAM_LEAD"].includes(role))) return tasks;
  return tasks.filter((task) => task.assignee_id === user.id || task.created_by === user.id ||
    (user.roles.includes("QA") && task.qa_reviewer_id === user.id));
}

export function attentionDeadline(tasks: TaskSummary[], userId: string, now: Date, overdue = false):
  { task: TaskSummary; overdue: boolean; remainingMs: number } | null {
  const candidates = tasks.filter((task) => deadlineStatuses.has(task.status) && task.due_cutoff_at &&
    (task.assignee_id === userId || (!task.assignee_id && task.created_by === userId)))
    .map((task) => ({ task, remainingMs: Date.parse(task.due_cutoff_at!) - now.getTime() }))
    .filter((item) => Number.isFinite(item.remainingMs) && (overdue
      ? item.remainingMs <= 0 : item.remainingMs > 0 && item.remainingMs < 24 * 60 * 60 * 1000))
    .sort((a, b) => overdue ? b.remainingMs - a.remainingMs : a.remainingMs - b.remainingMs);
  return candidates.length ? { ...candidates[0], overdue } : null;
}

export function deadlineCounts(tasks: TaskSummary[], now: Date) {
  const done = tasks.filter((task) => task.status === "DONE" && task.completed_at && task.due_cutoff_at);
  const onTime = done.filter((task) => Date.parse(task.completed_at!) < Date.parse(task.due_cutoff_at!)).length;
  const overdueOpen = tasks.filter((task) => deadlineStatuses.has(task.status) && task.due_cutoff_at &&
    Date.parse(task.due_cutoff_at) <= now.getTime()).length;
  return { onTime, late: done.length - onTime, overdueOpen };
}

export function completionDays(tasks: TaskSummary[], now: Date) {
  const today = tashkentDateKey(now);
  const [year, month, day] = today.split("-").map(Number);
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(Date.UTC(year, month - 1, day - (6 - index)));
    const key = date.toISOString().slice(0, 10);
    return { key, label: new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", timeZone: "UTC" }).format(date),
      count: tasks.filter((task) => task.status === "DONE" && task.completed_at &&
        tashkentDateKey(new Date(task.completed_at)) === key).length };
  });
}
