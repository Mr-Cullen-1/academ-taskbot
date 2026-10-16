# Taskbot

Taskbot has two user interfaces: a Telegram bot for registration, notifications and quick actions, and one Next.js Telegram Mini App for project and task work. FastAPI provides their shared business rules. The Phase 4 worker is opt-in. Product rules are in [`docs/`](docs/SPECIFICATION.md).

## Requirements and setup

- Python 3.11+, Node.js 20.9+, PostgreSQL, and a Telegram bot token for polling and Mini App login.
- Run commands from the repository root unless a command changes directory.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
Copy-Item apps\web\.env.local.example apps\web\.env.local
```

Set a real local `TASKBOT_DATABASE_URL` in `.env`; use `postgresql+asyncpg://user:password@localhost:5432/taskbot`. The database and role must exist first. `.env` and `apps/web/.env.local` are ignored by Git. Example files contain no credentials.

## Database and API

Apply the migrations. Revision `0001_phase2_access` creates identity and project access tables; `0002_phase3_tasks` creates task, comment, event and task-key sequence tables; `0003_phase4_notifications` adds notifications, delivery attempts and weekly report snapshots.

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m uvicorn taskbot_api.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET /api/v1/health` checks the process; `GET /api/v1/health/ready` checks the database.
- `GET /docs` exposes the FastAPI endpoint schema.
- Authenticated mutations require `X-CSRF-Token`; ordinary Phase 2 mutations also require an `Idempotency-Key` of 8–120 characters. Reusing a key with a different body returns 409.
- `GET /api/v1/users/me`, `GET /api/v1/users`, `GET /api/v1/users/pending`, `PATCH /api/v1/users/{id}/status`, and `PUT /api/v1/users/{id}/roles` cover profile and administration. User list, decisions and role changes require Super Admin.
- `GET/POST /api/v1/projects`, `GET /api/v1/projects/{id}`, `GET/PUT /api/v1/projects/{id}/members` provide minimal project access. Super Admin and PM may create; a PM sees only member projects. Only Super Admin changes membership pending a decision on OQ-02. The project owner is its creator and must remain a member.
- Approval uses `PATCH /users/{id}/status` with `{"status":"ACTIVE","roles":["PM"],"expected_version":1}`. Rejection uses `REJECTED`. Blocking uses `BLOCKED`; unblocking uses `ACTIVE` without `roles`. A blocked user's sessions are revoked, and audit history is retained. Rejected applicants cannot reapply until the owner settles OQ-03.
- The five accepted role codes are `SUPER_ADMIN`, `PM`, `TEAM_LEAD`, `SUBJECT_DEVELOPER`, `QA`; the public role endpoint cannot grant `SUPER_ADMIN` or create custom roles.

## Task workflow

- `GET/POST /api/v1/tasks`, `GET/PATCH /api/v1/tasks/{id}` list, create and edit tasks. List filters include `project_id`, `status`, `assignee_id`, `created_by`, `qa_reviewer_id` and `priority`.
- `PUT /api/v1/tasks/{id}/qa-reviewer` changes the separate QA reviewer; only the immutable task author may call it. Reviewers must have the QA role and active project membership. A QA reviewer cannot also be the main assignee.
- `POST /api/v1/tasks/{id}/{action}` handles `accept`, `reject`, `start`, `block`, `resume`, `submit-review`, `approve`, `admin-approve`, `request-rework`, `qa-review/done`, and `cancel`. Each request includes `expected_version` and an `Idempotency-Key`. Reasons are mandatory for `reject`, `block`, `cancel`, and `admin-approve`; `request-rework` requires `comment`. `admin-approve` also requires `author_unavailable_confirmed=true`.
- `POST/GET /api/v1/tasks/{id}/comments`, `GET /api/v1/tasks/{id}/events`, and `GET /api/v1/tasks/{id}/qa-reviews` expose comments and immutable history to authorized project members. A task change, its event and a generic outbox intent commit together; exceptional acceptance also writes an audit entry in the same transaction. The Phase 4 worker dispatches these intents when run explicitly.
- Status cannot be changed through PATCH. `QA DONE` is recorded in `qa_review_status`, while the task remains `REVIEW` until author or exceptional Super Admin acceptance. `created_by` is immutable. `OVERDUE` is never a task status.
- The due field is a required calendar date (`due_date` on create, `original_due_date` and `current_due_date` on read). `overdue` becomes true from 00:00 the following day in Asia/Tashkent for `OPEN`, `ACCEPTED`, `IN_PROGRESS`, `BLOCKED` and `REWORK`; `REVIEW`, `REJECTED`, `DONE` and `CANCELLED` are excluded. Time zone data comes from the Python `tzdata` dependency on Windows. Direct due-date edits and deadline extensions are reserved for later decisions. Reassignment currently works only while a task is `OPEN`; the exact post-rejection reassignment transition remains open in OQ-04.

## Telegram bot and first administrator

Set `TASKBOT_BOT_TOKEN`, then start polling:

```powershell
.venv\Scripts\python.exe -m taskbot_bot --check
.venv\Scripts\python.exe -m taskbot_bot
```

`--check` loads handlers without contacting Telegram. Polling requires the migrated PostgreSQL database. In a private chat, `/start` asks for the built-in contact share button, checks `contact.user_id == from_user.id`, then asks for a name and writes one `PENDING` account. The database deduplicates `update_id`; repeating registration never creates another user. With `TASKBOT_WEBAPP_URL` configured, an active user's `/start` reply includes an **Открыть задачи** Mini App button. Pending requests are available to Super Admin in the API and Mini App.

The bot supports `/my_tasks`, `/created_tasks`, `/task TASK-1001`, `/comments TASK-1001`, `/history TASK-1001`, and `/comment TASK-1001 text`. The detail response lists available commands such as `/accept`, `/begin`, `/block`, `/resume`, `/submit`, `/rework`, `/qa_done`, `/approve`, and `/cancel`. Reasons are included after the key; exceptional acceptance requires `/admin_approve TASK-1001 CONFIRM reason`. Assignment messages have **Принять** and **Отказаться** buttons; refusal requires a text reply with the reason. These actions require an active account and project access and are deduplicated by Telegram `update_id`.

**First Super Admin:** Have the intended administrator complete this bot registration. Then, on a trusted machine with database access, explicitly run:

```powershell
.venv\Scripts\python.exe -m taskbot_api.admin --telegram-id 123456789
```

Replace the ID with the verified pending Telegram ID from the administrator's request. This command succeeds only for an existing `PENDING` account and only when no Super Admin exists. It writes an audit entry. Never run it for an unknown request. Assign `TEAM_LEAD` to the appropriate verified account using the normal role endpoint or the administration interface; no person's name or number is embedded in code.

## Telegram Mini App

The existing Next.js frontend is the **single Mini App UI**, not a separate user website. Configure a public HTTPS address for it in `TASKBOT_WEBAPP_URL`, and set `TASKBOT_WEB_ORIGIN` to that address's exact origin. Set the server-only `TASKBOT_API_UPSTREAM` in `apps/web/.env.local` to the FastAPI HTTPS origin; leave `NEXT_PUBLIC_TASKBOT_API_URL` unset. Production startup requires this variable and rejects an HTTP upstream. Next.js proxies `/api/v1` on the Mini App's own origin, so its HttpOnly `Secure`, `SameSite=Lax` session cookie and CSRF requests remain same-origin. Keep the bot token only in the API/bot environment, never in a `NEXT_PUBLIC_` variable. A valid public URL must also be configured for the bot's Mini App button; optionally configure the same URL with BotFather `/setmenubutton`.

The frontend loads Telegram's official Web App script and sends the **raw** `Telegram.WebApp.initData` to `POST /api/v1/auth/webapp`. FastAPI verifies the Telegram HMAC, `auth_date` (default maximum age 300 seconds), input shape and one-time proof use, then finds the existing user by verified Telegram ID. Only `ACTIVE` users receive the existing revocable server session and a CSRF token. On reopening, `POST /api/v1/auth/session/refresh` restores a CSRF token only from a valid, active cookie session and the exact configured Origin; otherwise a fresh Telegram proof is required. `PENDING`, `BLOCKED`, rejected and unregistered users cannot access tasks. Every later API action retains backend role and project checks. The old Login Widget API remains for compatibility, but the Mini App UI does not load that widget. Direct browser access without Telegram `initData` has no production login UI. Deployment should enforce HTTPS and rate limits for login requests. See the [Telegram Mini Apps validation guide](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).

### Local development login without a Telegram domain

Use this only on your own computer with the already registered, `ACTIVE` Super Admin. Generate a new random secret locally:

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the output in the ignored root `.env` file, alongside the working database URL. The Telegram ID is read **only from server configuration**; the browser cannot choose an account:

```dotenv
TASKBOT_APP_ENV=development
TASKBOT_DEV_LOGIN_ENABLED=true
TASKBOT_DEV_LOGIN_TELEGRAM_ID=6362882059
TASKBOT_DEV_LOGIN_SECRET=<paste-generated-secret>
TASKBOT_WEB_ORIGIN=http://localhost:3000
TASKBOT_SESSION_COOKIE_SECURE=false
TASKBOT_NOTIFICATION_DELIVERY_ENABLED=false
```

In `apps/web/.env.local`, set `NEXT_PUBLIC_TASKBOT_API_URL=http://localhost:8000` and `NEXT_PUBLIC_TASKBOT_DEV_LOGIN_ENABLED=true` for this **local-only** path. Restart both servers, bind the API to `127.0.0.1`, open **http://localhost:3000**, and enter the local secret. This direct loopback API path preserves the dev route's loopback/proxy checks; with dev login disabled, Mini App traffic uses the same-origin rewrite. The API confirms the configured ID belongs to an existing active Super Admin and issues the usual cookie session and CSRF token. No user or role is created.

The route accepts only loopback clients, local Host and Origin, and no proxy forwarding headers. It is excluded entirely when `TASKBOT_DEV_LOGIN_ENABLED=false` (the default). With `TASKBOT_APP_ENV=production`, attempting to enable it fails configuration validation. To verify locally, restart the API with dev login disabled and confirm `POST /api/v1/auth/dev` returns `404`. Do not copy the local secret into Git, Next.js public variables, or a deployed environment.

### Start the Mini App frontend locally

With development login disabled, Next.js proxies `/api/v1/*` to `http://127.0.0.1:8000` by default. You can set `TASKBOT_API_UPSTREAM` in `apps/web/.env.local` to choose another local API origin. In production this variable is required and must be an HTTPS API origin. Keep it server-side; it is not a `NEXT_PUBLIC_` setting. Open the page in an ordinary browser to see the unauthenticated prompt; project and task data remain hidden without a valid session.

```powershell
Set-Location apps/web
npm.cmd install
npm.cmd run dev
```

The Mini App displays accessible projects and tasks, filters, permitted actions, comments and history; Super Admin also sees pending requests and a project creation form. For local browser development without Telegram, use the guarded dev login above. To test **real** Mini App login, use an HTTPS URL reachable by Telegram, configure the same URL in `TASKBOT_WEBAPP_URL`, and open the button in the bot from an approved account. `localhost` cannot serve as that public Telegram URL. On PowerShell systems that block `npm.ps1`, use `npm.cmd`.

## Worker and tests

The worker is a separate process. It consumes the transactional outbox, schedules deadline/reaction notifications, generates prior-week reports, and retries temporary delivery failures. Task changes remain atomic with their events and outbox rows. Telegram delivery is **off by default**; merely starting the API or bot never starts the worker.

Set `TASKBOT_NOTIFICATION_DELIVERY_ENABLED=false` for scheduling and inspection without sending. For a local simulated delivery run, set it to `true` and keep `TASKBOT_NOTIFICATION_TRANSPORT=dry_run`; results are recorded as `SIMULATED`. Real Telegram delivery requires an explicit `TASKBOT_NOTIFICATION_TRANSPORT=telegram`, `TASKBOT_NOTIFICATION_DELIVERY_ENABLED=true`, and a bot token. Do not enable that while testing with real participants. Inspect your `.env` before starting the worker.

The assignment notification names the author, task, description and date. If the assignee has not answered in one elapsed hour, the author and every active Team Lead with membership in the task's project receive one alert each. The assignee also gets a reminder after four accumulated working hours (Mon–Sat, 10:00–19:00 Asia/Tashkent; configurable through `TASKBOT_WORKDAYS`, `TASKBOT_WORKDAY_START`, `TASKBOT_WORKDAY_END`, and `TASKBOT_REACTION_WORK_MINUTES`). Acceptance, refusal, cancellation or reassignment stops obsolete reaction reminders. The assignee and author receive 24-hour, 2-hour and overdue reminders for eligible statuses. A changed due date invalidates old pending reminders.

Weekly reports cover the previous local calendar week and are generated on Monday at or after 09:00 Asia/Tashkent, with recovery on a later worker tick that week. Only active Super Admin (all projects) and active Team Lead (member projects) receive them; PM alone does not. The same stored report is available via `GET /api/v1/reports/weekly?week_start=YYYY-MM-DD`. `GET /api/v1/notifications` returns only the authenticated user's delivery log. Deadline-change requests and personal reminders are not implemented yet.

```powershell
.venv\Scripts\python.exe -m taskbot_worker --check
.venv\Scripts\python.exe -m taskbot_worker --once
.venv\Scripts\python.exe -m taskbot_worker --run
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Backend tests use isolated in-memory SQLite, fake delivery transport and generated Telegram signatures; bot handler tests mock replies and never call Telegram. Migration tests use disposable SQLite, while Alembic offline SQL checks PostgreSQL DDL generation. Run `alembic upgrade head` against a disposable PostgreSQL database before production use. `npm.cmd run build` checks the Mini App build. A real Telegram Mini App flow requires the configured bot and a reachable HTTPS URL.
