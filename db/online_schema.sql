-- Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
-- Программный комплекс «Честная Генерация»
-- Модуль детерминированной аналитики и LLM-аннотации
-- Автор: Саввиди Александр Анатольевич | ИНН 4725009270
--
-- Конфиденциальная информация. Несанкционированное использование запрещено.

-- Онлайн-мониторинг: Этап 1.5 (v2.2.0)
-- Применять: psql -U analytics -d analytics -f online_schema.sql

-- ── Пул наблюдаемых машин ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS online_observations (
    id                SERIAL      PRIMARY KEY,
    router_sn         TEXT        NOT NULL,
    equip_type        TEXT        NOT NULL,
    panel_id          INT         NOT NULL,
    -- Стартовая точка batch-добора (откуда начать при первом запуске)
    start_date        TIMESTAMPTZ NOT NULL,
    -- running / stopped
    status            TEXT        NOT NULL DEFAULT 'stopped'
                      CHECK (status IN ('running', 'stopped')),
    poll_interval_sec INT         NOT NULL DEFAULT 30,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (router_sn, equip_type, panel_id)
);

-- ── Лента автосегментов ───────────────────────────────────────────────────────
-- Отдельное хранилище от analysis_runs: непрерывная автоматическая лента.
-- t_end IS NULL → открытый (текущий) сегмент.
CREATE TABLE IF NOT EXISTS auto_segments (
    id                   BIGSERIAL   PRIMARY KEY,
    router_sn            TEXT        NOT NULL,
    equip_type           TEXT        NOT NULL,
    panel_id             INT         NOT NULL,

    t_start              TIMESTAMPTZ NOT NULL,
    t_end                TIMESTAMPTZ,           -- NULL = открытый сегмент

    run_state            INT,
    -- Причина закрытия (NULL = открытый)
    cause_close          TEXT
                         CHECK (cause_close IN (
                             'RUN_STATE_CHANGE',
                             'DAILY_BOUNDARY',
                             'OPERATOR_STOP'
                         ) OR cause_close IS NULL),
    -- Для суточного реза
    split_reason         TEXT,                  -- 'DAILY_BOUNDARY' если применимо
    continued_from       BIGINT REFERENCES auto_segments(id),
    continues_to         BIGINT REFERENCES auto_segments(id),

    -- coking_risk на момент закрытия (или текущий для открытого)
    coking_risk_json     JSONB,
    -- forward-fill память: только для сегментов, закрытых по DAILY_BOUNDARY (работа продолжается)
    forward_fill_json    JSONB,

    analytics_version    TEXT        NOT NULL DEFAULT '2.2.0',

    -- Для открытого сегмента: живая телеметрия
    current_values_json  JSONB,
    active_detections_json JSONB,

    -- Для закрытого сегмента: полный аналитический контракт
    characteristics_json JSONB,                -- Segment.to_dict()
    report_md            TEXT,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Поиск по машине (основной паттерн запросов)
CREATE INDEX IF NOT EXISTS idx_auto_seg_machine
    ON auto_segments (router_sn, equip_type, panel_id, t_start DESC);

-- В каждый момент времени у машины не более одного открытого сегмента
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_seg_one_open
    ON auto_segments (router_sn, equip_type, panel_id)
    WHERE t_end IS NULL;

-- Поиск по диапазону дат (для очистки, календаря)
CREATE INDEX IF NOT EXISTS idx_auto_seg_time
    ON auto_segments (t_start, t_end);

-- Защита от повторной вставки закрытого сегмента с тем же t_start
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_seg_closed_t_start
    ON auto_segments (router_sn, equip_type, panel_id, t_start)
    WHERE t_end IS NOT NULL;

-- Миграция v2.2.6: фиксированная правая граница batch-добора
ALTER TABLE online_observations
    ADD COLUMN IF NOT EXISTS batch_end_ts TIMESTAMPTZ;

-- Миграция v3.3.0: ИИ-оператор Уровень 1 — статус-строка
ALTER TABLE auto_segments
    ADD COLUMN IF NOT EXISTS status_text        TEXT,
    ADD COLUMN IF NOT EXISTS status_hash        TEXT,
    ADD COLUMN IF NOT EXISTS status_updated_at  TIMESTAMPTZ;

-- Миграция v4.0.0: Claude-анализ предупреждений онлайн
ALTER TABLE auto_segments
    ADD COLUMN IF NOT EXISTS warning_analysis_md   TEXT,
    ADD COLUMN IF NOT EXISTS warning_analyzed_hash TEXT;

-- Миграция v4.8.0: структурная форма статуса для карточек внешнего UI
-- (режим, время в режиме, текст тревоги — без парсинга status_text)
ALTER TABLE auto_segments
    ADD COLUMN IF NOT EXISTS status_struct_json JSONB;

-- Миграция v4.2.0: гейт предупреждений Claude — подавление ложных срабатываний аналитики
-- gate_suppressed_hash: хэш состава аналитических детекций, отменённых вердиктом Claude;
--   действует пока состав не изменился и сегмент открыт.
-- gate_log: append-only журнал решений гейта (вердикты, обоснования, токены).
ALTER TABLE auto_segments
    ADD COLUMN IF NOT EXISTS gate_suppressed_hash TEXT,
    ADD COLUMN IF NOT EXISTS gate_log             JSONB;

-- Миграция v4.9.0: универсальный счётчик срабатываний детекторов (Addendum v1.6)
-- Одна строка = одно событие (фронт) — один сегмент с данным сценарием детектора.
-- severity / run_state опциональны: заполняются для статистики паттернов.
-- ON DELETE CASCADE: удаление сегмента удаляет его события.
CREATE TABLE IF NOT EXISTS detection_events (
    id          BIGSERIAL   PRIMARY KEY,
    router_sn   TEXT        NOT NULL,
    equip_type  TEXT        NOT NULL,
    panel_id    INT         NOT NULL,
    scenario    TEXT        NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    segment_id  BIGINT      REFERENCES auto_segments(id) ON DELETE CASCADE,
    severity    TEXT,
    run_state   INT,
    front_count INT         NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_detection_events_lookup
    ON detection_events (router_sn, equip_type, panel_id, scenario, detected_at DESC);

-- Миграция v4.8.3: front_count — число фактических переходов порога в сегменте
ALTER TABLE detection_events
    ADD COLUMN IF NOT EXISTS front_count INT NOT NULL DEFAULT 1;

-- Миграция v4.9.1: журнал жизненного цикла детекций (живые тревоги)
-- Одна строка = одно событие перехода состояния детекции:
--   OPENED  — сценарий появился в активных детекциях
--   UPDATED — severity изменился (YELLOW↔RED)
--   CLOSED  — условие снято, сценарий исчез из детекций последнего подсегмента
-- Тревога живёт сквозь смены RUN_STATE; CLOSED — только при фактическом снятии условия.
-- ON DELETE SET NULL: удаление сегмента не удаляет историю журнала.
CREATE TABLE IF NOT EXISTS alert_journal (
    id           BIGSERIAL   PRIMARY KEY,
    router_sn    TEXT        NOT NULL,
    equip_type   TEXT        NOT NULL,
    panel_id     INT         NOT NULL,
    scenario     TEXT        NOT NULL,
    event_type   TEXT        NOT NULL
                 CHECK (event_type IN ('OPENED', 'UPDATED', 'CLOSED')),
    ts           TIMESTAMPTZ NOT NULL,
    severity     TEXT,
    trigger_text TEXT,
    values_json  JSONB,
    segment_id   BIGINT      REFERENCES auto_segments(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_journal_lookup
    ON alert_journal (router_sn, equip_type, panel_id, scenario, ts DESC);

-- Миграция v3.4.0: курсор синхронизации history из источника
CREATE TABLE IF NOT EXISTS history_sync_state (
    router_sn    TEXT        NOT NULL,
    equip_type   TEXT        NOT NULL,
    panel_id     INT         NOT NULL,
    last_sync_at TIMESTAMPTZ NOT NULL DEFAULT (now() - interval '7 days'),
    PRIMARY KEY (router_sn, equip_type, panel_id)
);

-- Миграция v4.9.14: свежесть телеметрии — максимальный ts строки history,
-- виденной движком этой машины (для data_stale в /api/machines и статус-строки)
ALTER TABLE online_observations
    ADD COLUMN IF NOT EXISTS last_data_ts TIMESTAMPTZ;

-- Миграция v4.9.16: эпизоды тревог — одна строка = один непрерывный эпизод
-- (панель или аналитика). Открыт: t_close IS NULL. Эпизоды уровня машины,
-- суточный рез их НЕ закрывает. active_sec тикает только по времени с данными
-- (в дырах связи эпизод висит, таймер стоит).
CREATE TABLE IF NOT EXISTS alarm_episodes (
    id               BIGSERIAL   PRIMARY KEY,
    router_sn        TEXT        NOT NULL,
    equip_type       TEXT        NOT NULL,
    panel_id         INT         NOT NULL,
    scenario         TEXT        NOT NULL,
    source           TEXT        NOT NULL DEFAULT 'analytics'
                     CHECK (source IN ('panel', 'analytics')),
    -- Максимальный severity за время жизни эпизода
    severity         TEXT,
    t_open           TIMESTAMPTZ NOT NULL,
    t_close          TIMESTAMPTZ,
    close_reason     TEXT,
    -- Длительность «под связью», сек: в дырах не тикает
    active_sec       DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Вердикт гейта Claude «отменить»: эпизод живёт и меряется, но из severity исключён
    gate_suppressed  BOOLEAN     NOT NULL DEFAULT FALSE,
    -- Снапшот detection.values на момент открытия
    open_values_json JSONB,
    -- Контекст аварии (Фаза C, только панельный SHUTDOWN)
    context_json     JSONB,
    segment_id_open  BIGINT      REFERENCES auto_segments(id) ON DELETE SET NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alarm_episodes_machine
    ON alarm_episodes (router_sn, equip_type, panel_id, t_open DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_episodes_open
    ON alarm_episodes (router_sn, equip_type, panel_id) WHERE t_close IS NULL;

-- Миграция v4.9.18: верхняя часть отчёта сегмента — вердикт, замечания
-- (эпизоды), ключевые показатели. report_md не меняется (полный отчёт,
-- UI сворачивает его как «Технические данные»).
ALTER TABLE auto_segments
    ADD COLUMN IF NOT EXISTS report_summary_md TEXT;

-- Миграция v4.9.22: per-fault ключ панельных аварий. Один scenario
-- CONTROLLER_FAULT покрывает все биты панели — чтобы аварии не затирали
-- друг друга (severity, счётчики, журнал), эпизод ключуется по (scenario,
-- addr, bit). addr/bit NULL — для аналитических сценариев (они уникальны).
ALTER TABLE alarm_episodes
    ADD COLUMN IF NOT EXISTS addr INT,
    ADD COLUMN IF NOT EXISTS bit  INT;

-- Миграция v4.9.36: история разборов гейта. warning_analysis_md перезаписывался
-- при каждой смене состава тревог (сброс + кнопка останова затирали разбор
-- исходной аварии). warning_analyses — append-only массив
-- {t, fault_hash, alarm_text, md}; warning_analysis_md остаётся «последним»
-- для обратной совместимости.
ALTER TABLE auto_segments
    ADD COLUMN IF NOT EXISTS warning_analyses JSONB;
