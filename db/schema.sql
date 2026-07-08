-- Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
-- Программный комплекс «Честная Генерация»
-- Модуль детерминированной аналитики и LLM-аннотации
-- Автор: Саввиди Александр Анатольевич | ИНН 4725009270
--
-- Конфиденциальная информация. Несанкционированное использование запрещено.

-- Схема аналитической БД cg-analytics
-- Применять: psql -U analytics -d analytics -f schema.sql

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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
    -- Папка в knowledge_base/equipment/ (legacy, монолитная привязка)
    kb_path         TEXT,
    -- Слоистая привязка: библиотеки controllers/<id> × engines/<id>.
    -- Если заданы обе — используются вместо kb_path (см. analytics/binding.py).
    controller_id   TEXT,
    engine_id       TEXT,
    -- true = участвует в ежедневной генерации отчётов
    active          BOOLEAN     NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (router_sn, equip_type, panel_id)
);

-- Миграция: добавить kb_path если таблица уже существует
ALTER TABLE equipment_registry ADD COLUMN IF NOT EXISTS kb_path TEXT;
-- Миграция (слоистая привязка): пара «контроллер × двигатель»
ALTER TABLE equipment_registry ADD COLUMN IF NOT EXISTS controller_id TEXT;
ALTER TABLE equipment_registry ADD COLUMN IF NOT EXISTS engine_id     TEXT;

-- Данные: перевести известную монолитную привязку KTA50/PCC3300 на пару.
-- Идемпотентно (guard по controller_id IS NULL). kb_path сохраняется как fallback.
UPDATE equipment_registry
   SET controller_id = 'pcc3300', engine_id = 'cummins_kta50', updated_at = now()
 WHERE kb_path = 'cummins_kta50_pcc3300'
   AND controller_id IS NULL AND engine_id IS NULL;

-- ── Аналитические прогоны v2 ─────────────────────────────────────────────────
-- Хранит полный контракт аналитики (JSON) + Markdown-отчёт за произвольный период.
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Идентификатор ГУ
    router_sn           TEXT        NOT NULL,
    equip_type          TEXT        NOT NULL,
    panel_id            INT         NOT NULL,
    engine_sn           TEXT,
    -- Запрошенный период
    ts_from             TIMESTAMPTZ NOT NULL,
    ts_to               TIMESTAMPTZ NOT NULL,
    -- Версия аналитического модуля
    analytics_version   TEXT        NOT NULL DEFAULT '2.0.0',
    -- Полный результат (контракт ТЗ Этап 1)
    segments_json       JSONB,
    -- Markdown-отчёт для человека и LLM
    report_md           TEXT,
    -- Сводные цифры (денормализованы для быстрого чтения)
    segments_count      INT,
    detections_count    INT,
    max_severity        TEXT CHECK (max_severity IN ('SHUTDOWN','ALARM','WARNING','INFO') OR max_severity IS NULL),
    data_quality_avg    NUMERIC(4,3),
    -- Служебные
    duration_ms         INT,        -- время выполнения прогона в мс
    error               TEXT,       -- текст ошибки (NULL = успех)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Уникальность: один прогон на ГУ + период
    UNIQUE (router_sn, equip_type, panel_id, ts_from, ts_to)
);

CREATE INDEX IF NOT EXISTS idx_analysis_runs_equipment
    ON analysis_runs (router_sn, equip_type, panel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_period
    ON analysis_runs (ts_from, ts_to);

-- ── Настройки приложения ──────────────────────────────────────────────────────
-- Пары ключ-значение, редактируемые через Web UI без перезапуска сервиса.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
