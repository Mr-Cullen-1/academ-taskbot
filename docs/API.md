# API — контракт Phase 0

Все пути ниже имеют префикс /api/v1. Бот и Telegram Mini App используют одну бизнес-логику backend; Next.js служит единственным интерфейсом Mini App. UUID передаются строками. Время — RFC 3339 с UTC offset; интерфейс переводит его в Asia/Tashkent. Статусы и переходы — [BUSINESS_RULES.md](BUSINESS_RULES.md), права — [PERMISSIONS.md](PERMISSIONS.md).

## Общие правила

- Каждый endpoint, кроме входа и Telegram webhook, требует действующую серверную сессию пользователя Active. Роль и проектный доступ проверяются на каждом запросе; списки фильтруются до пагинации. Недоступный объект возвращается как NOT_FOUND, если иначе раскрывалось бы его существование.
- Обычные изменяющие операции API используют Idempotency-Key, ограниченный актором и операцией; повтор с другим телом даёт 409 CONFLICT. Auth endpoints защищаются отдельно: POST /auth/webapp проверяет подписанный raw initData, auth_date, Origin и одноразовость proof; существующий POST /auth/telegram проверяет challenge и replay; POST /auth/session/refresh требует действующую cookie-сессию Active пользователя и точный Origin. Idempotency-Key не заменяет эти проверки. Telegram webhook защищается проверкой источника и атомарной дедупликацией update_id до побочного эффекта.
- Изменения существующего ресурса используют expected_version в теле либо If-Match; один механизм должен быть выбран перед реализацией. В таблицах ниже указан expected_version как рекомендуемый вариант. Для решения deadline_requests это версия запроса; связанная задача также блокируется и проверяется в транзакции. Несовпадение — 409 CONFLICT без новых task_events/outbox.
- Страницы списков: limit и cursor, стабильная сортировка (created_at и UUID как tie-breaker), серверный cap. Рекомендуемые 25/100 требуют проверки нагрузки; это техническая рекомендация. Фильтры перечислены ниже, произвольные SQL-поля не допускаются.
- Успех: GET 200, создание 201, действия 200 с обновлённым ресурсом или 204 для удаления/прочтения. Полный формат ответа: data и, для списков, page.next_cursor. Поля, недоступные роли, исключаются.
- Ошибка: {"error":{"code":"...","message":"...","details":{},"request_id":"UUID"}}. Коды: UNAUTHENTICATED (401), ACCOUNT_PENDING (403), ACCOUNT_BLOCKED (403), FORBIDDEN (403), NOT_FOUND (404), VALIDATION_ERROR (422), INVALID_STATUS_TRANSITION (409), DEADLINE_CONFLICT (409), DEPENDENCY_BLOCKED (409), CONFLICT (409). Дополнительно RATE_LIMITED (429) и INTERNAL_ERROR (500) как технические коды. Секреты и внутренние трассировки не возвращаются.

## Аутентификация и пользователи

| Операция | Минимальный запрос → ответ | Доступ / особое правило |
| --- | --- | --- |
| POST /auth/webapp | {init_data: raw Telegram.WebApp.initData} → HttpOnly app session, CSRF token и user summary | Основной вход Mini App; сервер проверяет HMAC по bot token, auth_date, Origin, одноразовость proof; только существующий Active user. Pending/Blocked/Rejected не получают сессию. |
| POST /auth/telegram | Legacy Login Widget proof → app session и user summary | Оставлен для совместимости API; Mini App его не вызывает. Проверка challenge, HMAC, срока и replay сохраняется. |
| POST /auth/session/refresh | Cookie действующей сессии и точный Origin → новый CSRF token | Для повторного открытия Mini App; требует Active пользователя, не продлевает срок сессии и не создаёт новую. Без cookie или с отозванной/истёкшей сессией вход выполняется по новому проверенному initData. |
| POST /auth/dev | {secret} → обычная cookie-сессия и CSRF token | Только явно включённый local development: loopback Host/client, точный локальный Origin, без proxy headers; ID берётся из конфигурации и проверяется как существующий Active SUPER_ADMIN. Маршрут отсутствует при выключенном режиме и не может быть включён с APP_ENV=production. Не является способом входа в развёрнутой системе. |
| POST /auth/logout | Пустой запрос → 204 | Владелец сессии; отзывает текущую сессию. |
| GET /users/me | — → профиль, account_status, эффективные роли/проекты | Авторизованный пользователь. |
| GET /users | Фильтр status/role/project, cursor → пользователи | Super Admin; PM получает список Active участников доступного проекта через GET /projects/{id} для назначения, без глобального списка пользователей |
| PATCH /users/{id}/status | {status, reason?, expected_version} → user | Единственный Super Admin; подтверждение Pending→Active, отклонение, блокировка. При Blocked отозвать сессии и сохранить инвариант одного Super Admin. Иные переходы — OQ-03. |
| PUT /users/{id}/roles | {roles:[code],expected_version} → роли | Только пять фиксированных кодов SUPER_ADMIN, PM, TEAM_LEAD, SUBJECT_DEVELOPER, QA. Назначает Super Admin; нельзя создать второго действующего SUPER_ADMIN или передать произвольные права. Полная замена набора, атомарно, с audit_log. |

Регистрация через Telegram начинается после /start: пользователь передаёт контакт штатной кнопкой и вводит имя. После проверки, что contact.user_id совпадает с from.id в проверенном Telegram update, сохраняется заявка Pending без рабочей сессии. Только Super Admin переводит её в Active. Бот сейчас использует polling и дедупликацию update_id; webhook с secret header и TLS остаётся возможным будущим способом приёма updates. Вход Mini App не подтверждает номер телефона.

## Проекты и спринты

| Операция | Минимальный запрос → ответ | Доступ |
| --- | --- | --- |
| GET /projects | status, cursor → видимые проекты | Участник проекта или Super Admin; роль PM сама по себе не расширяет список |
| POST /projects | {name,description,owner_user_id?} → project | Active Super Admin или PM по фиксированной роли; по умолчанию owner_user_id=создатель, создатель становится участником, иной владелец обязан быть Active участником; Team Lead, Предметник-разработчик и QA не могут создать проект |
| GET /projects/{id} | — → project, разрешённые участники | Член или Super Admin |
| PATCH /projects/{id} | {name?,description?,status?,expected_version} → project | Super Admin; право PM на изменение метаданных требует OQ-02; допустимые статусы — OQ-06 |
| PUT /projects/{id}/members | {members:[user_id],expected_version} → состав | Super Admin; право PM на изменение состава требует OQ-02; полная замена, нельзя неявно оставить задачу с неразрешённым assignee |
| GET /projects/{id}/sprints | status, cursor → sprints | Член или Super Admin |
| POST /projects/{id}/sprints | {name,starts_at,ends_at,status?} → sprint | Super Admin либо PM с членством в проекте |
| PATCH /sprints/{id} | {name?,starts_at?,ends_at?,status?,expected_version} → sprint | Super Admin либо PM с членством в проекте; project_id неизменяем |

Статусы проектов и спринтов, их допустимые изменения и точность дат требуют решения OQ-06.
Для согласованных команд проекта необходимы GET/POST /projects/{id}/teams и PUT /teams/{id}/members: просмотр, создание и замена состава команды. Доступ к просмотру у участников проекта, изменение у уполномоченного PM/Super Admin; состав команды должен быть подмножеством project_members. Эти операции добавлены потому, что команды прямо входят в предметную область, хотя исходный список API был предварительным.

Полномочия определяются одной или несколькими из пяти фиксированных ролей, статусом Active, членством в проекте и отношением к задаче как created_by/assignee_id. Индивидуальные grants, ACL и произвольные поля разрешений в API отсутствуют. Роль PM даёт создание проекта, но управление или чтение существующего проекта по-прежнему требует членства; роль Team Lead не даёт административных прав Super Admin. Пределы отдельных проектных действий, не заданные владельцем, описаны в [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).

## Задачи и переходы

GET /tasks поддерживает project_id, sprint_id, assignee_id, created_by, status, priority, type, overdue, due_from/due_to и cursor. Возвращает только видимые задачи. POST /tasks доступен любому Active участнику проекта и принимает project_id, sprint_id?, title, description, assignee_id?, priority, type, due_date; сервер присваивает id, task_key, неизменяемый created_by, OPEN, original_due_date=current_due_date=due_date и version. Автор вправе указать assignee_id при создании, но только одного Active участника того же проекта; при отсутствии assignee OPEN ждёт назначения PM и accept недоступен. Дедлайн обязателен, все поля срока являются календарными датами YYYY-MM-DD без времени суток. Поля ответа срока называются original_due_date и current_due_date; OVERDUE вычисляется с 00:00 следующего дня Asia/Tashkent для OPEN, ACCEPTED, IN_PROGRESS, BLOCKED и REWORK, остальные статусы исключены. GET /tasks/{id} возвращает задачу. PATCH /tasks/{id} доступен PM с членством в проекте и меняет только разрешённые поля title/description/sprint_id/priority/type и, после решения OQ-05, current_due_date; assignee_id после создания устанавливается/меняется PM в доступном проекте и только на Active участника этого проекта. Super Admin использует PM-операции с задачей только при наличии у него также роли PM и проектного доступа, кроме прямо утверждённого admin-approve. status и created_by через PATCH запрещены. Изменение срока обязано оставить deadline_change/event; прямое изменение и отношение к pending запросу — OQ-05. Переназначение REJECTED и его следующий статус — OQ-04. Смена assignee проверяет проектное членство и создаёт событие/уведомление.

POST /tasks также может принять qa_reviewer_id?: автор выбирает отдельного QA-проверяющего из Active участников проекта с фиксированной ролью QA; он не становится основным assignee. Поля ответа включают qa_reviewer_id и qa_review_status (NULL без QA; PENDING/REWORK/DONE при назначении). PUT /tasks/{id}/qa-reviewer с {qa_reviewer_id?,expected_version} позволяет только неизменяемому автору назначить, заменить или снять QA у незавершённой задачи; изменение сбрасывает текущий qa_review_status в PENDING либо NULL и сохраняет прежнее значение и актора в task_event. Проверка статуса и проектного доступа выполняется backend. QA может действовать только будучи текущим qa_reviewer_id и только в REVIEW. QA DONE — результат проверки, а не изменение tasks.status; автор может выполнить обычное approve и до QA DONE. После общего DONE QA-действия отклоняются, а незавершённая QA-проверка не записывается как пройденная.

GET /tasks дополнительно фильтрует по qa_reviewer_id и qa_review_status в пределах видимых проектов, чтобы назначенный QA видел свои проверки; фильтр не расширяет проектный доступ.

QA-проверка использует те же expected_version, Idempotency-Key и атомарную запись task_event/outbox, что и другие мутации задачи. Событие QA DONE содержит фактического QA, прежний/новый qa_review_status и время; оно не получает approval_mode=AUTHOR/ADMIN_OVERRIDE и не устанавливает completed_at. QA-возврат записывает обязательный комментарий и фактического актора. Никакой дополнительный канал уведомлений только из QA DONE не предполагается до отдельного правила; существующее уведомление REWORK сохраняется.

| Операция | Из → в | Тело | Актор |
| --- | --- | --- | --- |
| POST /tasks/{id}/accept | OPEN → ACCEPTED | expected_version | Текущий assignee |
| POST /tasks/{id}/reject | OPEN → REJECTED | reason, expected_version | Текущий assignee; reason обязателен |
| POST /tasks/{id}/start | ACCEPTED или REWORK → IN_PROGRESS | expected_version | Текущий assignee |
| POST /tasks/{id}/block | IN_PROGRESS → BLOCKED | reason, expected_version | Текущий assignee; reason обязателен |
| POST /tasks/{id}/resume | BLOCKED → IN_PROGRESS | expected_version | Текущий assignee; необходимый endpoint для согласованного перехода |
| POST /tasks/{id}/submit-review | IN_PROGRESS → REVIEW | result/comment?, expected_version | Текущий assignee; формат результата OQ-04 |
| POST /tasks/{id}/approve | REVIEW → DONE | expected_version | Неизменяемый created_by |
| POST /tasks/{id}/admin-approve | REVIEW → DONE | author_unavailable_confirmed=true, reason, expected_version | Только Super Admin; явное подтверждение недоступности и непустая причина обязательны |
| POST /tasks/{id}/request-rework | REVIEW → REWORK | comment, expected_version | Неизменяемый created_by либо назначенный для проверки QA; comment обязателен; сохраняется фактический актор |
| POST /tasks/{id}/qa-review/done | REVIEW → REVIEW; qa_review_status: PENDING → DONE | expected_version | Только назначенный qa_reviewer_id с ролью QA и доступом к проекту; не устанавливает completed_at и не заменяет approve |
| POST /tasks/{id}/cancel | OPEN, ACCEPTED, REJECTED, IN_PROGRESS, BLOCKED, REVIEW, REWORK → CANCELLED | reason, expected_version | created_by или PM с членством в проекте |

Ни один переход не выполняется PATCH статуса. После каждого повторного submit-review текущий qa_review_status назначенного QA сбрасывается в PENDING, а предшествующий результат остаётся в task_events; request-rework от QA устанавливает qa_review_status=REWORK. Обычный approve не требует причины вмешательства и доступен только created_by, в том числе при ещё не завершённой QA-проверке. admin-approve — отдельное действие: backend проверяет REVIEW, единственную роль Super Admin, author_unavailable_confirmed=true и непустую reason; отсутствие любого условия отклоняет операцию без изменения задачи. Отсутствие активности или ответа автора само по себе не разрешает исключение. При успехе tasks.created_by неизменен, completed_at фиксирует DONE тем же транзакционным моментом, что и occurred_at события; task_event получает approval_mode=ADMIN_OVERRIDE, фактический actor_user_id, original_author_user_id, author_unavailable_confirmed=true и intervention_reason; связанный audit_log сохраняет те же сведения. Обычное действие пишет approval_mode=AUTHOR и фактического автора. Оба пути создают предусмотренное событием DONE уведомление исполнителю, а не дублируют его. Успех возвращает задачу с новым version и событие в истории. Запрещённый переход — INVALID_STATUS_TRANSITION; неисполненное условие исключения — FORBIDDEN или VALIDATION_ERROR по причине отказа без раскрытия лишних данных. Правило зависимостей для start/submit-review/approve/admin-approve пока OQ-07.

## Дедлайны, обсуждение, связи

| Операция | Минимальный запрос → ответ | Доступ |
| --- | --- | --- |
| POST /tasks/{id}/deadline-requests | {proposed_due_date,reason,progress_note,expected_version} → request Pending | Текущий assignee; все три содержательных поля обязательны |
| POST /deadline-requests/{id}/approve | {expected_version} → request Approved и task.current_due_date | created_by или уполномоченный PM; создаёт deadline_change, event, outbox атомарно |
| POST /deadline-requests/{id}/reject | {reason?,expected_version} → request Rejected | created_by или уполномоченный PM; старый срок остаётся |
| POST /tasks/{id}/comments | {body} → comment | Участник с правом комментировать |
| GET /tasks/{id}/comments | cursor → comments | Читатель задачи, с учётом видимости комментариев |
| POST /tasks/{id}/watchers | {user_id} → watcher | Участник проекта добавляет только себя; добавление других наблюдателей требует отдельного решения, пользователь должен быть Active участником проекта |
| GET /tasks/{id}/watchers | cursor → watchers | Читатель задачи |
| DELETE /tasks/{id}/watchers/{userId} | — → 204 | Участник проекта удаляет только себя; снятие другого наблюдателя требует отдельного решения |
| POST /tasks/{id}/dependencies | {depends_on_task_id} → dependency | Право редактирования задачи; проверка self/cycle на backend |
| GET /tasks/{id}/events | cursor → упорядоченная история | Участник проекта или Super Admin; чувствительные значения доступны только в утверждённом объёме |
| POST /tasks/{id}/attachments | Метаданные файла или запрос на приватную загрузку → attachment/upload instruction | Необходим для согласованных вложений; точный поток OQ-12 |
| GET /tasks/{id}/attachments | cursor → метаданные вложений | Читатель задачи |
| GET /tasks/{id}/attachments/{attachmentId}/download | — → краткоживущая приватная ссылка | Только авторизованный читатель вложения |

Удаление зависимости и вложения, если разрешено продуктом, должно иметь отдельный контракт после OQ-07/OQ-12; это не предполагается автоматически.

## Уведомления, напоминания, аналитика и аудит

| Операция | Запрос → ответ | Доступ |
| --- | --- | --- |
| GET /notifications | state, cursor → мои уведомления | Только получатель |
| POST /notifications/{id}/read | — → 200/204 | Только получатель, повтор безопасен |
| GET /reminders | cursor → личные напоминания | Только владелец Active, независимо от одной из пяти ролей |
| POST /reminders | {task_id?,remind_at,text?} → reminder | Только владелец Active; task_id, если есть, должен быть ему доступен |
| PATCH /reminders/{id} | {remind_at?,text?,expected_version} → reminder | Только владелец Active |
| DELETE /reminders/{id} | — → 204 | Только владелец Active |
| GET /reports/weekly | week_start? → сохранённый отчёт за предыдущую календарную неделю; 404 до генерации worker | Active Super Admin: все проекты; Active Team Lead: только проекты с членством. PM без одной из этих ролей не имеет доступа. В Phase 4 сравнение недель и project_id-фильтр не реализованы |
| GET /reports/tasks | period/project/assignee/status → агрегаты | PM своего проекта или Super Admin |
| GET /reports/users | period/project → рабочие показатели без рейтинга | PM своего проекта или Super Admin |
| GET /audit-logs | actor/action/time/cursor → записи доступа/администрирования | Только Super Admin; расширение доступа не предполагается |

Для notifications получатель берётся из сохранённой записи, а не из параметра клиента; чужие уведомления не возвращаются списком и не помечаются прочитанными по известному UUID. Для reminders owner_user_id устанавливается по текущей сессии и не принимается из тела запроса; чтение, изменение и удаление чужого напоминания запрещены.

В Phase 4 реализовано только чтение собственных notifications и недельного отчёта. Endpoint-ы отметки прочтения, личных reminders и остальные аналитические отчёты остаются будущей работой. Уведомление о назначении содержит две кнопки Telegram; отказ завершается только после ввода причины, переход проверяет текущего исполнителя, статус OPEN и проектный доступ, повтор update_id дедуплицируется.

Определения метрик едины для всех поверхностей и зафиксированы в [BUSINESS_RULES.md](BUSINESS_RULES.md). Показатель принятых автором берёт только task_event с approval_mode=AUTHOR; завершённые DONE и доля соблюдения срока включают также утверждённую исключительную приёмку Super Admin, различимую по approval_mode=ADMIN_OVERRIDE. Отчёт не содержит лидерборд.

## Защита, повтор и транзакции

Для [Mini App](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app) сервер проверяет raw initData: отсортированную строку полей, HMAC-SHA-256 с ключом HMAC-SHA-256(bot token, key `WebAppData`), auth_date, структуру user.id, Origin и replay. initDataUnsafe не используется для аутентификации. После обмена действуют существующие cookie-сессия, CSRF и backend-проверки ролей и проектного доступа. Для cookie-запросов Mini App и API должны быть на одном origin через проксирование /api/v1; в production нужны HTTPS и Secure/SameSite=Lax. Legacy Login Widget остаётся только для совместимости API. Контакт для регистрации проверяется отдельно в Telegram update бота.

Cookie-сессия, если выбрана: HttpOnly, Secure, SameSite, CSRF token, проверка Origin, отзыв при Blocked; bearer-модель требует безопасного хранения и ротации. Ни один токен или raw auth payload не пишется в логи. Login и мутации ограничиваются rate limit. Файлы проверяются по размеру и типу; объекты приватны. Секреты только в окружении.

Процедура мутации: authentication → Active → authorization → validation → atomic idempotency claim → version check → task/deadline state + task_event + outbox + logical notification → commit. При admin-approve связанный audit_log входит в эту же транзакцию. Ошибка или отказ не оставляет частично записанных данных. Для webhook update_id также дедуплицируется до побочного эффекта. Worker повторяет временно неудачные отправки и фиксирует каждую попытку; неоднозначный таймаут не даёт абсолютной гарантии однократной физической доставки.
