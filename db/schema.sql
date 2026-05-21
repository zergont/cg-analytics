-- Схема аналитической БД cg-analytics
-- Применять: psql -U analytics -d analytics -f schema.sql

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ── Реестр оборудования ───────────────────────────────────────────────────────
-- Копия метаданных из основной БД + флаг участия в ежедневном анализе.
-- Заполняется автоматически при первом запуске, редактируется через Web UI.
CREATE TABLE IF NOT EXISTS equipment_registry (
    router_sn       TEXT        NOT NULL,
    equip_type      TEXT        NOT NULL,
    panel_id        INT         NOT NULL,
    name            TEXT,
    manufacturer    TEXT,
    model           TEXT,
    engine_sn       TEXT,
    -- Папка в knowledge_base/equipment/ (назначается вручную)
    kb_path         TEXT,
    -- true = участвует в ежедневной генерации отчётов
    active          BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (router_sn, equip_type, panel_id)
);

-- ── Суточные отчёты ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_reports (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Дата отчёта (UTC)
    date             DATE        NOT NULL,
    -- Идентификатор ГУ
    router_sn        TEXT        NOT NULL,
    equip_type       TEXT        NOT NULL,
    panel_id         INT         NOT NULL,
    -- Метаданные оборудования на момент создания отчёта
    manufacturer     TEXT,
    model            TEXT,
    engine_sn        TEXT,
    -- Общий статус: ok / attention / critical
    status           TEXT        NOT NULL CHECK (status IN ('ok', 'attention', 'critical')),
    -- Наработка и пуски
    uptime_minutes   INTEGER,
    starts_count     INTEGER,
    -- Список аномалий с деталями (структурированный JSON)
    anomalies        JSONB,
    -- Агрегированные значения всех регистров за сутки
    -- Используется в v2 для анализа трендов деградации
    aggregates       JSONB,
    -- Отчёт от агента (plain text / markdown)
    ai_report        TEXT,
    -- Версия модели (для отслеживания изменений качества при апгрейде)
    ai_model         TEXT,
    tokens_used      INTEGER,
    tool_calls_count INTEGER,
    -- Время генерации отчёта в секундах
    generation_time_sec NUMERIC(8,2),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Один отчёт на ГУ в сутки
    UNIQUE (date, router_sn, equip_type, panel_id)
);

CREATE INDEX IF NOT EXISTS idx_reports_date      ON daily_reports (date DESC);
CREATE INDEX IF NOT EXISTS idx_reports_equipment ON daily_reports (router_sn, equip_type, panel_id);
CREATE INDEX IF NOT EXISTS idx_reports_status    ON daily_reports (status, date DESC);

-- Миграция: добавить kb_path если таблица уже существует
ALTER TABLE equipment_registry ADD COLUMN IF NOT EXISTS kb_path TEXT;

-- ── Настройки приложения ──────────────────────────────────────────────────────
-- Пары ключ-значение, редактируемые через Web UI без перезапуска сервиса.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
