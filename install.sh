#!/usr/bin/env bash
# =============================================================================
# cg-analytics — скрипт первичной установки
# Запуск: bash install.sh
# Поддерживаемые ОС: Ubuntu 22.04 / 24.04
# Скрипт идемпотентен — безопасно запускать повторно.
# =============================================================================

set -euo pipefail

# ── Цвета для вывода ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}  →${NC}  $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
fail() { echo -e "${RED}  ✗  $*${NC}"; exit 1; }
step() { echo -e "\n${BOLD}${CYAN}▶ $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BOLD}"
echo "  ╔═══════════════════════════════════╗"
echo "  ║        cg-analytics install       ║"
echo "  ║   Честная Генерация — v1.0        ║"
echo "  ╚═══════════════════════════════════╝"
echo -e "${NC}"

# =============================================================================
# 1. Python
# =============================================================================
step "Проверка Python"

PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(sys.version_info[:2])")
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            ok "Найден $cmd ($VER)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    warn "Python 3.11+ не найден. Устанавливаю python3.12..."
    sudo apt-get update -qq
    sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
    PYTHON=python3.12
    ok "Python 3.12 установлен"
fi

# =============================================================================
# 2. Системные зависимости
# =============================================================================
step "Системные зависимости"

# Все нужные пакеты одной командой
APT_PKGS=(python3.12-venv python3.12-dev libpq-dev build-essential postgresql postgresql-16-pgvector)

MISSING_PKGS=()
for pkg in "${APT_PKGS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null 2>&1; then
        MISSING_PKGS+=("$pkg")
    fi
done

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    info "Устанавливаю: ${MISSING_PKGS[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${MISSING_PKGS[@]}"
    ok "Системные пакеты установлены"
else
    ok "Все системные пакеты уже есть"
fi

# =============================================================================
# 3. Виртуальное окружение
# =============================================================================
step "Виртуальное окружение Python"

if [[ ! -d ".venv" ]]; then
    info "Создаю .venv..."
    "$PYTHON" -m venv .venv
    ok "Виртуальное окружение создано"
else
    ok ".venv уже существует"
fi

VENV_PYTHON=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

# =============================================================================
# 4. Python-зависимости
# =============================================================================
step "Python-зависимости (pip install)"

info "Обновляю pip..."
"$VENV_PIP" install --upgrade pip --quiet

info "Устанавливаю зависимости из requirements.txt..."
"$VENV_PIP" install -r requirements.txt --quiet
ok "Зависимости установлены"

# =============================================================================
# 5. Конфигурация
# =============================================================================
step "Конфигурация"

if [[ ! -f "config.yml" ]]; then
    cp config.example.yml config.yml
    warn "Создан config.yml из шаблона."
    echo ""
    echo -e "  ${BOLD}Откройте config.yml и заполните обязательные поля:${NC}"
    echo -e "  ${YELLOW}  databases.source${NC}    — строка подключения к основной БД"
    echo -e "  ${YELLOW}  databases.analytics${NC} — строка подключения к аналитической БД"
    echo -e "  ${YELLOW}  anthropic.api_key${NC}   — ваш ключ Anthropic API"
    echo ""
    read -rp "  Нажмите Enter когда config.yml будет заполнен..." _
else
    ok "config.yml уже существует"
fi

# Проверка: не остались ли плейсхолдеры
if grep -q "sk-ant-api03-\.\.\." config.yml 2>/dev/null; then
    fail "В config.yml остался плейсхолдер anthropic.api_key. Заполните config.yml и запустите скрипт снова."
fi
if grep -q "user:pass@" config.yml 2>/dev/null; then
    fail "В config.yml остались плейсхолдеры databases. Заполните config.yml и запустите скрипт снова."
fi
ok "Конфигурация выглядит заполненной"

# =============================================================================
# 6. Применение схемы аналитической БД
# =============================================================================
step "Схема аналитической БД"

# Расширения требуют суперпользователя — создаём отдельно от имени postgres
info "Создаю расширения pgvector и pgcrypto (требуется sudo)..."
if sudo -u postgres psql -d analytics -c "
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
" 2>&1; then
    ok "Расширения созданы"
else
    fail "Не удалось создать расширения. Убедитесь что postgresql-16-pgvector установлен и PostgreSQL запущен."
fi

# Применяем схему (таблицы) от имени пользователя analytics
info "Применяю db/schema.sql к analytics DB..."
if "$VENV_PYTHON" - <<'PYEOF'
import asyncio, sys
try:
    from db.analytics import init_db
    asyncio.run(init_db())
    print("  OK")
except Exception as e:
    print(f"  ERR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
then
    ok "Схема БД применена"
else
    fail "Не удалось применить схему БД. Проверьте databases.analytics в config.yml и доступность PostgreSQL."
fi

# =============================================================================
# 7. Проверка Ollama / LMStudio
# =============================================================================
step "Проверка embedding-сервиса"

EMBED_URL=$("$VENV_PYTHON" -c "from config import settings; print(settings.embedding_base_url)" 2>/dev/null || echo "http://localhost:11434")
EMBED_MODEL=$("$VENV_PYTHON" -c "from config import settings; print(settings.embedding_model)" 2>/dev/null || echo "nomic-embed-text")

if curl -sf --max-time 3 "${EMBED_URL}" -o /dev/null 2>/dev/null || \
   curl -sf --max-time 3 "${EMBED_URL}/api/tags" -o /dev/null 2>/dev/null; then
    ok "Embedding-сервис доступен: $EMBED_URL"

    # Проверим наличие нужной модели (только для Ollama)
    if echo "$EMBED_URL" | grep -q "11434"; then
        if curl -sf "${EMBED_URL}/api/tags" 2>/dev/null | grep -q "$EMBED_MODEL"; then
            ok "Модель $EMBED_MODEL загружена"
        else
            warn "Модель $EMBED_MODEL не найдена в Ollama."
            read -rp "  Загрузить сейчас? (ollama pull $EMBED_MODEL) [Y/n]: " PULL
            if [[ "${PULL:-Y}" =~ ^[Yy]$ ]]; then
                ollama pull "$EMBED_MODEL"
                ok "Модель $EMBED_MODEL загружена"
            else
                warn "Пропущено. Запустите 'ollama pull $EMBED_MODEL' вручную перед индексацией."
            fi
        fi
    fi
else
    warn "Embedding-сервис недоступен по адресу $EMBED_URL"
    warn "Установите Ollama: curl -fsSL https://ollama.com/install.sh | sh && ollama pull $EMBED_MODEL"
    warn "Индексация knowledge base будет невозможна до запуска embedding-сервиса."
fi

# =============================================================================
# 8. Индексация knowledge base (если есть файлы)
# =============================================================================
step "Knowledge Base"

KB_PATH=$("$VENV_PYTHON" -c "from config import settings; print(settings.knowledge_base_path)" 2>/dev/null || echo "./knowledge_base")
EQUIPMENT_DIR="${KB_PATH}/equipment"

if [[ -d "$EQUIPMENT_DIR" ]] && find "$EQUIPMENT_DIR" -name "register_map.jsonl" | grep -q .; then
    MODEL_COUNT=$(find "$EQUIPMENT_DIR" -name "register_map.jsonl" | wc -l)
    info "Найдено $MODEL_COUNT модель(ей) оборудования."
    read -rp "  Запустить первичную индексацию? [Y/n]: " IDX
    if [[ "${IDX:-Y}" =~ ^[Yy]$ ]]; then
        info "Индексация..."
        if "$VENV_PYTHON" -m knowledge.indexer --all; then
            ok "Индексация завершена"
        else
            warn "Индексация завершилась с ошибками (возможно, embedding-сервис недоступен)."
            warn "Запустите вручную: .venv/bin/python -m knowledge.indexer --all"
        fi
    else
        info "Пропущено. Запустите позже: .venv/bin/python -m knowledge.indexer --all"
    fi
else
    info "Папка knowledge_base/equipment/ пуста."
    info "Добавьте файлы и запустите: .venv/bin/python -m knowledge.indexer --all"
fi

# =============================================================================
# 9. Systemd-сервис (опционально)
# =============================================================================
step "Systemd-сервис"

if [[ -d /etc/systemd/system ]] && command -v systemctl &>/dev/null; then
    read -rp "  Установить cg-analytics как systemd-сервис? [y/N]: " SYSTEMD
    if [[ "${SYSTEMD:-N}" =~ ^[Yy]$ ]]; then
        # Создать системного пользователя если не существует
        if ! id cg-analytics &>/dev/null; then
            sudo useradd --system --no-create-home --shell /sbin/nologin cg-analytics
            ok "Пользователь cg-analytics создан"
        fi

        # Подставить реальный путь в unit-файл
        INSTALL_DIR="$SCRIPT_DIR"
        sudo sed "s|/opt/cg-analytics|${INSTALL_DIR}|g" cg-analytics.service \
            | sudo tee /etc/systemd/system/cg-analytics.service > /dev/null

        sudo chown -R cg-analytics:cg-analytics "$INSTALL_DIR"
        sudo systemctl daemon-reload
        sudo systemctl enable cg-analytics
        sudo systemctl start cg-analytics
        ok "Сервис установлен и запущен"
        info "Логи: journalctl -u cg-analytics -f"
    else
        info "Пропущено. Запуск вручную: .venv/bin/python main.py"
    fi
else
    info "systemd не обнаружен. Запуск: .venv/bin/python main.py"
fi

# =============================================================================
# Готово
# =============================================================================
WEB_PORT=$("$VENV_PYTHON" -c "from config import settings; print(settings.web_port)" 2>/dev/null || echo "8090")

echo ""
echo -e "${GREEN}${BOLD}  ══════════════════════════════════════"
echo -e "   Установка завершена успешно!"
echo -e "  ══════════════════════════════════════${NC}"
echo ""
echo -e "  Web UI:    ${CYAN}http://localhost:${WEB_PORT}${NC}"
echo -e "  Запуск:    ${CYAN}.venv/bin/python main.py${NC}"
echo -e "  Индекс:    ${CYAN}.venv/bin/python -m knowledge.indexer --all${NC}"
echo -e "  Логи:      ${CYAN}journalctl -u cg-analytics -f${NC}"
echo ""
