-- Copyright (c) 2026 ООО «НГ-ЭНЕРГОСЕРВИС». Все права защищены.
-- Программный комплекс «Честная Генерация»
-- Модуль детерминированной аналитики и LLM-аннотации
-- Автор: Саввиди Александр Анатольевич | ИНН 4725009270
--
-- Конфиденциальная информация. Несанкционированное использование запрещено.

-- Этап 2: корпус Claude-разметки сегментов (v3.0.0)
-- Применять: psql -U analytics -d analytics -f corpus_schema.sql

CREATE TABLE IF NOT EXISTS segment_analyses (
    id                  BIGSERIAL   PRIMARY KEY,
    auto_segment_id     BIGINT      NOT NULL UNIQUE REFERENCES auto_segments(id) ON DELETE CASCADE,

    -- Состояние очереди / воркера
    status              TEXT        NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'processing', 'done', 'error')),

    -- Результат Claude
    conclusion_md       TEXT,           -- структурированный markdown (Блоки 1+2+3)
    humanized_md        TEXT,           -- проза qwen для UI оператора

    -- Денормализованные поля для фильтрации без парсинга md
    verdict             TEXT,           -- норма / отклонение / требует_внимания
    alarm_level         TEXT,           -- нет / INFO / WARNING / ALARM / SHUTDOWN

    -- Версионирование
    claude_model        TEXT,
    analytics_version   TEXT,

    -- Статистика (дублируется из debug_json для быстрых запросов)
    tokens_used         INT,
    tool_calls_count    INT,
    loops_count         INT,
    generation_time_sec NUMERIC(8, 2),

    -- Полный трейс для отладки
    debug_json          JSONB,

    -- Ошибка (NULL = успех)
    error               TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_seg_analyses_segment
    ON segment_analyses (auto_segment_id);

CREATE INDEX IF NOT EXISTS idx_seg_analyses_status
    ON segment_analyses (status);

CREATE INDEX IF NOT EXISTS idx_seg_analyses_verdict
    ON segment_analyses (verdict, alarm_level);
