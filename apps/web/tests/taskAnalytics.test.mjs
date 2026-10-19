import test from "node:test";
import assert from "node:assert/strict";
import { attentionDeadline, completionDays, dashboardTasks, deadlineCounts, tashkentDateKey } from "../app/taskAnalytics.ts";

const base = {
  task_key: "TASK-1", title: "Task", project_id: "project", status: "OPEN", priority: "MEDIUM",
  created_by: "author", assignee_id: "worker", qa_reviewer_id: null,
  current_due_date: "2026-10-20", due_cutoff_at: "2026-10-20T19:00:00+00:00",
  completed_at: null, qa_review_status: null, overdue: false, created_at: null,
};
const task = (id, changes = {}) => ({ ...base, id, ...changes });

test("urgent warning uses the server cutoff, ownership, and current task state", () => {
  const now = new Date("2026-10-20T14:00:00Z");
  const rows = [task("later"), task("done", { status: "DONE", due_cutoff_at: "2026-10-20T15:00:00Z" }),
    task("review", { status: "REVIEW", due_cutoff_at: "2026-10-20T15:00:00Z" }),
    task("other", { assignee_id: "someone-else", due_cutoff_at: "2026-10-20T15:00:00Z" })];
  assert.equal(attentionDeadline(rows, "worker", now)?.task.id, "later");
  assert.equal(attentionDeadline(rows, "worker", now)?.remainingMs, 5 * 60 * 60 * 1000);
  assert.equal(attentionDeadline([task("late")], "worker", new Date("2026-10-20T19:00:01Z")), null);
  assert.equal(attentionDeadline([task("late")], "worker", new Date("2026-10-20T19:00:01Z"), true)?.overdue, true);
  assert.equal(attentionDeadline([task("late", { due_cutoff_at: "2026-10-19T19:00:00Z" }), task("soon")],
    "worker", now)?.task.id, "soon");
  assert.equal(attentionDeadline([task("cancelled", { status: "CANCELLED" })], "worker", now), null);
  assert.equal(attentionDeadline([task("unassigned", { assignee_id: null })], "author", now)?.task.id, "unassigned");
});

test("completed on time and late are separate from open overdue tasks", () => {
  const rows = [task("on-time", { status: "DONE", completed_at: "2026-10-20T18:59:59Z" }),
    task("late", { status: "DONE", completed_at: "2026-10-20T19:00:00Z" }),
    task("open"), task("review", { status: "REVIEW" }), task("cancelled", { status: "CANCELLED" })];
  assert.deepEqual(deadlineCounts(rows, new Date("2026-10-20T19:00:01Z")), {
    onTime: 1, late: 1, overdueOpen: 1,
  });
});

test("completion days use Asia/Tashkent calendar boundaries", () => {
  assert.equal(tashkentDateKey(new Date("2026-10-19T19:15:00Z")), "2026-10-20");
  const days = completionDays([task("done", { status: "DONE", completed_at: "2026-10-19T19:15:00Z" })],
    new Date("2026-10-20T12:00:00Z"));
  assert.equal(days.at(-1).key, "2026-10-20");
  assert.equal(days.at(-1).count, 1);
  assert.equal(days.reduce((sum, day) => sum + day.count, 0), 1);
});

test("individuals do not receive aggregate project metrics", () => {
  const rows = [task("mine"), task("other", { created_by: "other", assignee_id: "other" })];
  assert.equal(dashboardTasks(rows, { id: "worker", roles: ["SUBJECT_DEVELOPER"] }).length, 1);
  assert.equal(dashboardTasks(rows, { id: "lead", roles: ["TEAM_LEAD"] }).length, 2);
});
