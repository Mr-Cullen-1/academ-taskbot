# ERD — логическая модель Phase 0

Источник: согласованная спецификация, разделы 3–11 и 13, с последним утверждённым уточнением о пяти ролях, регистрации и доступе. Это логическая модель, а не миграция. Все внутренние первичные ключи — UUID; все временные метки — UTC. Набор обязательных сущностей согласован с [SPECIFICATION.md](SPECIFICATION.md), API — с [API.md](API.md).

## Кардинальности

- users M:N пяти фиксированным roles через user_roles; users M:N projects через project_members. Один users является владельцем каждого projects. В рабочем состоянии системы ровно один Active Super Admin.
- projects 1:N sprints, project_teams и tasks. У sprint один project; task.sprint_id допускает NULL и обязан ссылаться на тот же проект.
- project_teams M:N users через project_team_members; команда существует внутри одного проекта, её участник также является project_members этого проекта.
- tasks N:1 users как неизменяемый created_by; у задачи не более одного основного assignee_id. При создании автор может выбрать Active участника проекта либо оставить OPEN без assignee до назначения PM; до назначения assignee переход accept недоступен. Отдельный qa_reviewer_id? указывает одного Active участника проекта с ролью QA и не заменяет основного исполнителя. tasks M:N users через task_watchers.
- tasks 1:N comments, task_attachments, task_events, deadline_requests, notifications, reminders; tasks M:N tasks через task_dependencies.
- deadline_requests 1:0..1 deadline_changes при одобрении. deadline_changes и task_events сохраняют историю изменения сроков.
- task_events 1:N outbox_events; notifications 1:N notification_attempts. Outbox может порождать уведомления/отчёты.
- users 1:N sessions и idempotency_keys. external_links связываются с внутренним объектом разрешённого типа.

## Сущности и ключевые поля

| Таблица | Поля и связи | Назначение |
| --- | --- | --- |
| users | id, telegram_user_id UNIQUE, phone, name, account_status, version, created_at, updated_at, archived_at? | Pending — заявка без рабочего доступа; Active после подтверждения Super Admin; также Rejected, Blocked. Телефон не является ключом веб-входа. |
| roles | id, code UNIQUE | Ровно пять фиксированных кодов: SUPER_ADMIN, PM, TEAM_LEAD, SUBJECT_DEVELOPER, QA. Отдельного Observer или индивидуального permission code нет. |
| user_roles | id, user_id, role_id, granted_by, granted_at, revoked_at? | Несколько фиксированных ролей у пользователя. Роль не привязана к конкретному проекту: проектный доступ задаёт project_members. Ровно одно действующее назначение SUPER_ADMIN на систему; роль TEAM_LEAD назначается подтверждённому аккаунту Зарины Равшановны. |
| sessions | id, user_id, issued_at, expires_at, revoked_at?, credential_digest | Существующие серверные сессии используются Mini App после проверки raw initData; отзываются при Blocked. Новая таблица для входа Mini App не требуется. |
| projects | id, name, description, owner_user_id, status, version, created_at, updated_at, archived_at? | Одна компания, без tenant_id. Создатель становится участником проекта; назначенный владелец — Active участник проекта. |
| project_members | id, project_id, user_id, joined_at, left_at? | Доступ к проекту только для Active пользователя; одна текущая запись на пару. PM без членства не видит чужой проект. |
| project_teams | id, project_id, name, archived_at? | Команды внутри проекта. |
| project_team_members | id, team_id, user_id, joined_at, left_at? | Членство в команде при членстве в проекте. |
| sprints | id, project_id, name, starts_at, ends_at, status, version, archived_at? | Даты и статус; точный календарный формат в OQ-06. |
| tasks | id, task_key UNIQUE, project_id, sprint_id?, title, description, created_by, assignee_id?, qa_reviewer_id?, qa_review_status?, status, priority, type, original_due_date, current_due_date, created_at, updated_at, completed_at?, version, archived_at? | Не более одного основного исполнителя; автор может выбрать его при создании, позднее назначает PM. Автор выбирает QA-проверяющего отдельно. qa_review_status: NULL без QA, иначе PENDING/REWORK/DONE; это не tasks.status. created_by неизменяем. Обе даты срока обязательны, тип DATE. |
| task_dependencies | id, task_id, depends_on_task_id, created_by, created_at | UNIQUE(task_id, depends_on_task_id), запрет самоссылки и циклов. |
| task_watchers | id, task_id, user_id, added_at | UNIQUE(task_id, user_id); не соисполнители. |
| comments | id, task_id, author_user_id, body, created_at, archived_at? | Комментарии и обязательные причины переходов; связь с event при необходимости. |
| task_attachments | id, task_id, uploaded_by, object_key, filename, media_type, size_bytes, created_at, archived_at? | Только метаданные; объект приватен. |
| task_events | id, task_id, sequence, actor_user_id?, type, before_data, after_data, approval_mode?, original_author_user_id?, author_unavailable_confirmed?, intervention_reason?, occurred_at, request_id | Append-only история; UNIQUE(task_id, sequence). Для REVIEW→DONE approval_mode=AUTHOR или ADMIN_OVERRIDE; при ADMIN_OVERRIDE обязательны actor_user_id=фактический Super Admin, original_author_user_id=tasks.created_by, author_unavailable_confirmed=true и intervention_reason. |
| deadline_requests | id, task_id, requested_by, proposed_due_date, reason, progress_note, status, version, decided_by?, decided_at?, created_at | Pending, Approved, Rejected; до Approved текущий срок не меняется. |
| deadline_changes | id, task_id, request_id?, old_due_date, new_due_date, decided_by, decided_at, reason | История всех одобренных изменений; request_id UNIQUE если запрос был основанием. |
| reminders | id, owner_user_id, task_id?, remind_at, text?, state, version, created_at, updated_at | Личные напоминания отдельно от метрик и системных уведомлений. |
| notifications | id, task_id?, recipient_user_id, subject_user_id?, source_event_id?, kind, dedupe_key UNIQUE, body, scheduled_at, due_date_snapshot?, state, attempts, lease_until?, last_error_code?, sent_at?, created_at | Логическое уведомление Phase 4; адресат определяется сервером, снимок даты отсекает устаревшее напоминание. |
| notification_attempts | id, notification_id, attempted_at, result, error_code? | Журнал попыток; номер попытки хранится в notifications.attempts. |
| weekly_reports | id, recipient_user_id, week_start, generated_at, payload | UNIQUE(recipient_user_id, week_start); снимок недели для Telegram и API; Super Admin видит все проекты, Team Lead — доступные по членству. |
| outbox_events | id, aggregate_type, aggregate_id, source_event_id?, kind, payload, dedupe_key UNIQUE, state, available_at, created_at, processed_at? | Транзакционная запись для worker; без Telegram-вызова в транзакции задачи. |
| audit_logs | id, actor_user_id?, action, target_type, target_id?, source_event_id?, original_author_user_id?, author_unavailable_confirmed?, reason?, outcome, occurred_at, request_id, metadata | Операции доступа и администрации; ADMIN_OVERRIDE связан с task_event, хранит фактического актора, исходного автора, явное подтверждение, причину и время; без секретов. |
| external_links | id, provider, external_id, target_type, target_id, created_at | UNIQUE(provider, external_id), задел для будущей интеграции Notion. |
| idempotency_keys | id, actor_user_id, operation, key, request_hash, response_status, response_ref?, expires_at, created_at | UNIQUE(actor_user_id, operation, key); атомарное подавление повторных записей. |
| telegram_updates | id, update_id UNIQUE, received_at, processed_at?, state | Дедупликация updates бота при текущем polling; для будущего webhook нужна также проверка источника. |

## Ограничения и индексы

- FK для task.project_id и sprint.project_id; задача не может быть привязана к спринту иного проекта. assignee_id, created_by и watchers должны быть Active и иметь доступ к проекту в момент назначения/добавления; это бизнес-инвариант backend и транзакции, не только FK.
- user_roles и project_members — исчерпывающая модель выдачи прав MVP вместе с правилами автора/исполнителя. Индивидуальные grants, custom permissions и ACL отсутствуют. Фиксированная роль PM разрешает создание проекта, но чтение и управление существующим проектом требуют членства. Состав проекта до решения OQ-02 меняет только Super Admin. Любой Active участник с доступом к проекту может создать задачу, независимо от одной из четырёх неадминистративных ролей.
- Инвариант единственного Super Admin проверяется при каждой смене ролей/статуса и должен быть подкреплён физическим ограничением от конкурентного второго назначения в будущей схеме. Заявка Pending хранит имя и проверенный Telegram contact, но не получает рабочую сессию до Active.
- tasks.status при создании равен OPEN; дальнейшие значения: ACCEPTED, REJECTED, IN_PROGRESS, BLOCKED, REVIEW, REWORK, DONE, CANCELLED. OVERDUE вычисляется из current_due_date и состояния с 00:00 следующего дня Asia/Tashkent; REVIEW, REJECTED, DONE и CANCELLED исключены. OVERDUE не хранится как status. Типы и приоритеты перечислены в [BUSINESS_RULES.md](BUSINESS_RULES.md).
- qa_reviewer_id, если задан, должен иметь Active, членство в проекте и фиксированную роль QA в момент назначения и действия. Создание и изменение этого назначения выполняет created_by; изменение оставляет task_event. QA может установить qa_review_status=DONE при сохранении tasks.status=REVIEW либо вернуть REVIEW→REWORK с обязательным комментарием и qa_review_status=REWORK. Повторная отправка в REVIEW сбрасывает qa_review_status=PENDING и сохраняет прежние результаты в task_events. Финальная авторская/исключительная приёмка может произойти и при PENDING: такой результат не считается пройденной QA-проверкой. После tasks.status=DONE QA не меняет результат того же цикла.
- QA-переходы записывают actor_user_id, прежний/новый qa_review_status и, при REWORK, комментарий в task_events; approval_mode=AUTHOR/ADMIN_OVERRIDE относится только к общему REVIEW→DONE. Изменение QA и общей задачи сохраняется атомарно с version и outbox. QA DONE само по себе не устанавливает completed_at и не считается завершением для аналитики.
- Для REVIEW→DONE approval_mode обязателен. AUTHOR означает actor_user_id=tasks.created_by без intervention_reason; ADMIN_OVERRIDE означает действующую роль Super Admin, явное подтверждение им недоступности автора в момент действия, original_author_user_id=tasks.created_by и непустую intervention_reason. Отсутствие активности автора не является автоматическим критерием. Связанный audit_logs.source_event_id ссылается на событие исключительной приёмки. tasks.completed_at и task_events.occurred_at для этого перехода берутся из одного транзакционного момента; created_by не обновляется.
- completed_at устанавливается при переходе в DONE. Семантика исторической метрики после возможного изменения DONE остаётся вопросом; обратный переход из DONE не согласован.
- task_key уникален глобально; генератор должен выдерживать конкуренцию. Telegram ID и chat ID требуют типа, не усекающего более 32 бит.
- Индексы: tasks(project_id,status,current_due_date), tasks(assignee_id,status), tasks(created_by,created_at), tasks(sprint_id); project_members(project_id,user_id), deadline_requests(task_id,status), task_events(task_id,sequence), notifications(recipient_user_id,state,scheduled_at), outbox_events(state,available_at), audit_logs(occurred_at,actor_user_id). Физическая настройка после оценки запросов.
- Архивирование users/projects/tasks не должно каскадно стирать task_events, deadline_changes, audit_logs и попытки доставки. Логическая схема предлагает archived_at и запрет новых операций над архивными объектами; точные правила видимости и хранения — OQ-10.
- Уникальные ограничения на активное членство и роль должны учитывать left_at/revoked_at; способ частичных индексов определить в физической схеме. Схема не реализована.

## Атомарность

Мутация задачи, её task_event, deadline_change при одобрении, нужные notifications/outbox_events и итог idempotency_keys сохраняются одной транзакцией. При исключительной приёмке REVIEW→DONE в той же транзакции создаётся связанный audit_log; tasks.created_by остаётся неизменным, completed_at фиксирует время DONE. Обычная и исключительная приёмка имеют одинаковый статус результата, но различаются approval_mode для аудита и метрик. Повтор с тем же ключом возвращает исходный результат; другой payload с тем же ключом отклоняется. Worker обрабатывает outbox с блокировкой/claim и записывает попытки доставки. При неопределённом ответе Telegram точно-однократная физическая доставка не гарантируется; логическая дедупликация обязательна.

Нерешённые детали модели перечислены в [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).
