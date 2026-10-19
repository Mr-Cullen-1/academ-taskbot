import Link from "next/link";
import { completionDays, deadlineCounts, statusLabels, type TaskSummary } from "./taskAnalytics";

export default function DashboardCharts({ tasks, completionTasks, now, scopeLabel }: {
  tasks: TaskSummary[]; completionTasks: TaskSummary[]; now: Date; scopeLabel: string;
}) {
  const days = completionDays(completionTasks, now);
  const maximum = Math.max(1, ...days.map((day) => day.count));
  const statuses = Object.entries(statusLabels).map(([status, label]) => ({
    status, label, count: tasks.filter((task) => task.status === status).length,
  })).filter((item) => item.count > 0);
  const deadlines = deadlineCounts(completionTasks, now);
  const openDeadlines = deadlineCounts(tasks, now).overdueOpen;
  const completed = deadlines.onTime + deadlines.late;
  return <section className="analytics" aria-label="Показатели задач">
    <div className="section-heading compact"><h2>Показатели задач</h2></div>
    <div className="analytics-grid">
      <div className="surface chart-card"><h3>Завершено за 7 дней</h3><small>{scopeLabel}</small>
        {days.some((day) => day.count) ? <div className="day-chart" role="img"
          aria-label={days.map((day) => `${day.label}: ${day.count}`).join("; ")}>
          {days.map((day) => <div className="day-column" key={day.key}>
            <strong>{day.count}</strong><span className="day-bar" style={{ height: `${Math.max(4, day.count / maximum * 100)}%` }} />
            <small>{day.label}</small></div>)}
        </div> : <p className="chart-empty">За последние 7 дней завершённых задач нет.</p>}</div>
      <div className="surface chart-card"><h3>Текущие статусы</h3>
        {statuses.length ? <ul className="status-chart">{statuses.map((item) => <li key={item.status}>
          <Link href={`/tasks?status=${item.status}`}><span>{item.label}</span><strong>{item.count}</strong></Link>
          <div className="chart-track"><span style={{ width: `${item.count / tasks.length * 100}%` }} /></div>
        </li>)}</ul> : <p className="chart-empty">Задач в доступной области пока нет.</p>}</div>
      <div className="surface chart-card"><h3>Соблюдение сроков</h3>
        {completed ? <><div className="deadline-chart" role="img"
          aria-label={`В срок: ${deadlines.onTime}; с опозданием: ${deadlines.late}`}>
          <span style={{ width: `${deadlines.onTime / completed * 100}%` }} />
          <span style={{ width: `${deadlines.late / completed * 100}%` }} /></div>
          <p className="chart-legend"><span>В срок: {deadlines.onTime}</span><span>С опозданием: {deadlines.late}</span></p>
          <small>Из {completed} завершённых задач с датой срока</small></> :
          <p className="chart-empty">Недостаточно данных: завершённых задач пока нет.</p>}
        {openDeadlines > 0 && <Link className="text-link" href="/tasks">Сейчас просрочены: {openDeadlines}</Link>}
      </div>
    </div>
  </section>;
}
