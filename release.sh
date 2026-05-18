#!/usr/bin/env bash
# Поднять версию на 0.0.1, закоммитить и запушить с тегом.
# Использование: bash release.sh "Описание изменений"

set -euo pipefail

MSG="${1:-}"
if [[ -z "$MSG" ]]; then
    echo "Укажите описание: bash release.sh \"Что изменилось\""
    exit 1
fi

VERSION_FILE="$(dirname "$0")/VERSION"
CURRENT=$(cat "$VERSION_FILE" | tr -d '[:space:]')

# Разбить на части и поднять патч
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"
PATCH=$((PATCH + 1))
NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"

echo "$NEW_VERSION" > "$VERSION_FILE"
echo "Версия: $CURRENT → $NEW_VERSION"

git add -A
git commit -m "v${NEW_VERSION}: ${MSG}"
git tag "v${NEW_VERSION}"
git push
git push --tags

echo "Готово: v${NEW_VERSION}"
