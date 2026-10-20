# MVP requirement check (2026-10-20)

This matrix compares the current repository with Phase 0 and later owner decisions. “Implemented” refers to local code and isolated tests, not a deployed feature. The live Supabase schema was last reported at `0005_outbox_retry`; `0006_mvp_entities` has **not** been applied. DEMO is an ordinary project and is never filtered from task or Worker processing.

| Requirement | Repository status | Evidence / launch condition |
| --- | --- | --- |
| Telegram registration, contact ownership, Pending/Active/Rejected, single Super Admin | Implemented and tested | `registration.py`, `onboarding.py`, registration/API tests; live BotFather and recipient flow still needs a controlled smoke test. |
| Mini App `initData`, server session, CSRF, revocation | Implemented and tested | `security.py`, `routes.py`, `TaskbotApp.tsx`, auth tests. Production HTTPS origin and bot token must be configured. |
| Five fixed roles and project isolation, Team Lead global read | Implemented and tested | `security.py`, `routes.py`, `task_service.py`, role tests. No individual grants. |
| Projects and membership | Implemented and tested | Project API and Mini App; PM project administration remains OQ-02. |
| Project teams and sprint dates | Implemented locally | `planning_routes.py`, `PlanningPanel.tsx`, `0006_mvp_entities`, `test_mvp_planning.py`; needs migration and live UI check. Sprints have calendar dates and no status. |
| Tasks, types, priorities, one assignee, QA, transitions, comments, history | Implemented and tested | `task_service.py`, `task_routes.py`, `TaskBoard.tsx`, bot menu and task tests. |
| Deadline extension, original/approved date history, one Pending request | Implemented locally | `deadline_routes.py`, task detail and tests; needs migration and controlled notification check. Request goes to author, decision to assignee. |
| Watchers and same-project dependencies | Implemented locally | `task_links_routes.py`, task detail and tests; dependencies do not block transitions. Needs migration. |
| Private PDF/PNG/JPEG/TXT attachments, 10 MB, author/admin deletion | Implemented locally | `attachment_routes.py`, `attachment_storage.py`, task detail, mocked Storage tests; needs migration, private bucket, server-only key and live Storage smoke test. |
| Notification read state and personal reminders | Implemented locally | `notification_routes.py`, `reminder_routes.py`, `MvpPanels.tsx`, Worker relevance, tests; needs migration and live Worker smoke test. |
| Outbox, retries, stale lease protection, deadline and no-response scheduling | Implemented and isolated tests pass | Worker files and Phase 4 tests. No live queue processing or real Telegram delivery performed in this pass. |
| Weekly reports Monday 09:00 Asia/Tashkent | Implemented in code | Super Admin/Team Lead all projects; PM accessible projects. Weekly API, bot menu and Worker updated; isolated tests pass, live schedule not run. |
| Task and user analytics, project/assignee filtering | Implemented locally; historical attribution remains open | `analytics_routes.py`, `MvpPanels.tsx`, tests; created/completed previous-period comparison is period-based. Historical assignee attribution and zero-denominator display remain OQ-11. |
| Bot quick actions and full Mini App | Implemented locally within the agreed two-interface split | Bot supports registration, approval, task list/create/transition, projects, notifications, personal reminders, deadline extension, weekly report and current summary, profile/help, WebApp button. Mini App provides full planning, links, attachments and analytics. External Telegram UI smoke test remains. |
| Security and data privacy | Local checks implemented | Backend authorization, CSRF, active status, private Storage check and tests. Production secrets, origin, signed-download behavior, BotFather and Storage bucket need live validation. |
| Backup, restore, production launch | Runbook prepared; external setup pending | `DEPLOYMENT.md`; confirm backup/restore capability, apply migration by separate decision, set permanent HTTPS origins and secrets, build with real API HTTPS origin, then controlled smoke tests. |
| Notion integration, rankings, scores, levels | Excluded from MVP | No implementation planned. |

## Remaining owner decisions

See `OPEN_QUESTIONS.md`: PM editing project metadata/membership (OQ-02); resubmission and unblocking details (OQ-03); reassignment after rejection and result format (OQ-04); direct deadline edits/past dates (OQ-05); project status/archival (OQ-06); quiet hours and repeat overdue notifications (OQ-08); retention/erasure (OQ-10); zero-denominator and historical assignee attribution (OQ-11). These do not authorize extra role privileges or unsafe defaults.

## Deployment gate

Do not call the new capabilities live until `0006_mvp_entities` is reviewed and applied to the existing Supabase, the private Storage bucket and server-only credentials are configured, the production Next.js build succeeds with the real HTTPS API origin, and live Telegram/Mini App/Worker smoke tests are authorized. No current test or local build proves those external steps.
