# Данные для визуальной проверки Mini App

В подключённой через корневой `.env` базе Supabase PostgreSQL (`postgres`)
создан проект `[DEMO] Taskbot UI — проверка статусов и приоритетов`, ID:
`6aa4b085-3cff-4a7b-9a75-7893f5cbea30`.

В нём ровно 13 задач с префиксом `[DEMO]`: все 9 статусов задач и все 4
приоритета. Полные ID и ожидаемые статусы сохранены локально в
`.tmp/demo-supabase-manifest.json` (файл игнорируется Git). Проект доступен
зарегистрированному Super Admin и, по текущим правам, всем активным Team Lead;
другие участники в него не добавлялись.
История создана штатными переходами сервиса задач. QA-проверяющий не назначен:
это потребовало бы участия другого действующего пользователя или создания
фиктивного активного аккаунта. Задача без даты не создавалась, поскольку API
и утверждённые правила требуют дедлайн.

Worker не запускался. Доставка выключена (`TASKBOT_NOTIFICATION_DELIVERY_ENABLED=false`,
`TASKBOT_NOTIFICATION_TRANSPORT=dry_run`). Для DEMO-задач существуют 40
необработанных outbox-событий на момент предыдущего аудита. DEMO является
обычным рабочим проектом: после отдельного запуска Worker эти события,
уведомления, напоминания и отчёты обрабатываются по общим правилам.
DEMO-записи остаются на месте; описанную ниже очистку выполнять только после
отдельного решения владельца.

## Проверка в действующем Telegram Mini App

19 октября 2026 года проект и все 13 задач были видны в открытом Mini App под
действующим Super Admin. Список содержал все 9 статусов и 4 приоритета. Выбор
проекта и совместный фильтр `OPEN` + `URGENT` оставили одну соответствующую
задачу; фильтр `OPEN` + `HIGH` показал просроченную задачу. На странице
`TASK-1004` загрузились название, описание, статус, приоритет, срок, доступные
действия и событие `CREATED` в истории. На главной срочная задача и уже
просроченная задача появились в отдельных блоках со сроками в `Asia/Tashkent`.
Страница проекта показала `Задач: 13`, `Завершено: 2`. Открывались формы
создания задачи и проекта без сохранения, профиль и экран пользователей.
Графики завершения за 7 дней, текущих статусов и соблюдения сроков загрузились;
их показатели охватывают все доступные Super Admin проекты, включая рабочие.
Проверка действий от лица PM, Team Lead, QA и Subject Developer не выполнялась.
Данные проверены в существующем WebApp через временный Quick Tunnel; его URL
может измениться после перезапуска `cloudflared`.

## Безопасная очистка после проверки — не выполнялась

Остановите Worker. Перед выполнением в Supabase SQL Editor проверьте, что в
проекте по указанному ID по-прежнему находятся только эти 13 DEMO-задач.
Следующая транзакция сама прервётся, если название проекта, количество или
маркировка задач изменились. Она не затрагивает задачи других проектов.

```sql
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM projects
    WHERE id = '6aa4b085-3cff-4a7b-9a75-7893f5cbea30'::uuid
      AND name = '[DEMO] Taskbot UI — проверка статусов и приоритетов'
  ) THEN
    RAISE EXCEPTION 'DEMO project identity mismatch';
  END IF;
  IF (SELECT count(*) FROM tasks
      WHERE project_id = '6aa4b085-3cff-4a7b-9a75-7893f5cbea30'::uuid) <> 13
     OR EXISTS (
       SELECT 1 FROM tasks
       WHERE project_id = '6aa4b085-3cff-4a7b-9a75-7893f5cbea30'::uuid
         AND title NOT LIKE '[DEMO] %'
     ) THEN
    RAISE EXCEPTION 'DEMO task set changed; review before cleanup';
  END IF;
END $$;

CREATE TEMP TABLE demo_task_ids ON COMMIT DROP AS
SELECT id FROM tasks
WHERE project_id = '6aa4b085-3cff-4a7b-9a75-7893f5cbea30'::uuid;

DELETE FROM notification_attempts
WHERE notification_id IN (
  SELECT id FROM notifications
  WHERE task_id IN (SELECT id FROM demo_task_ids)
     OR source_event_id IN (
       SELECT id FROM task_events WHERE task_id IN (SELECT id FROM demo_task_ids)
     )
);
DELETE FROM notifications
WHERE task_id IN (SELECT id FROM demo_task_ids)
   OR source_event_id IN (
     SELECT id FROM task_events WHERE task_id IN (SELECT id FROM demo_task_ids)
   );
DELETE FROM outbox_events
WHERE source_event_id IN (
  SELECT id FROM task_events WHERE task_id IN (SELECT id FROM demo_task_ids)
);
DELETE FROM comments WHERE task_id IN (SELECT id FROM demo_task_ids);
DELETE FROM task_events WHERE task_id IN (SELECT id FROM demo_task_ids);
DELETE FROM tasks WHERE id IN (SELECT id FROM demo_task_ids);
DELETE FROM project_members
WHERE project_id = '6aa4b085-3cff-4a7b-9a75-7893f5cbea30'::uuid;
DELETE FROM projects
WHERE id = '6aa4b085-3cff-4a7b-9a75-7893f5cbea30'::uuid;

COMMIT;
```

Журнал аудита создания проекта и глобальный счётчик `TASK-*` намеренно не
сбрасываются: это сохраняет историю действий и уже выданные номера задач.
Если Worker успеет создать еженедельный отчёт с DEMO-проектом, перед
очисткой проверьте его отдельно: отчёт может включать и рабочие проекты.
