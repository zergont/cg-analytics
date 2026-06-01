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
