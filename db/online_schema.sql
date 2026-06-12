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

-- Миграция v3.4.0: курсор синхронизации history из источника
CREATE TABLE IF NOT EXISTS history_sync_state (
    router_sn    TEXT        NOT NULL,
    equip_type   TEXT        NOT NULL,
    panel_id     INT         NOT NULL,
    last_sync_at TIMESTAMPTZ NOT NULL DEFAULT (now() - interval '7 days'),
    PRIMARY KEY (router_sn, equip_type, panel_id)
);
