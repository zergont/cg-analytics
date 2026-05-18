"""RAG-запросы к pgvector-индексу: поиск релевантных описаний регистров и fault-кодов."""
import logging
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# Кэш инициализированных индексов
_index_cache: dict[tuple[str, str], Any] = {}


def retrieve_context(
    manufacturer: str,
    model: str,
    active_addrs: list[int],
    fault_addrs: list[int] | None = None,
    top_k: int = 20,
) -> str:
    """Извлечь релевантный контекст из knowledge base для промпта агента.

    Формирует запрос из активных адресов регистров и fault-кодов,
    возвращает текстовый блок для вставки в system prompt.
    """
    if not active_addrs:
        return ""

    try:
        index = _get_index(manufacturer, model)
    except Exception as e:
        logger.warning("RAG недоступен для %s/%s: %s", manufacturer, model, e)
        return ""

    # Формируем запрос из адресов активных регистров
    addr_list = ", ".join(str(a) for a in sorted(set(active_addrs))[:50])
    query = f"Описание регистров с адресами: {addr_list}"

    if fault_addrs:
        fault_list = ", ".join(str(a) for a in sorted(set(fault_addrs))[:20])
        query += f". Fault-коды из регистров: {fault_list}"

    try:
        retriever = index.as_retriever(similarity_top_k=top_k)
        nodes = retriever.retrieve(query)

        if not nodes:
            return ""

        sections = []
        for node in nodes:
            meta = node.metadata or {}
            doc_type = meta.get("type", "")

            if doc_type == "register":
                sections.append(f"[Регистр {meta.get('addr')}] {node.get_content()}")
            elif doc_type == "fault":
                sections.append(
                    f"[Fault addr={meta.get('addr')} bit={meta.get('bit')} "
                    f"severity={meta.get('severity')}] {node.get_content()}"
                )
            elif doc_type == "manual":
                sections.append(
                    f"[РЭ {meta.get('source')} стр.{meta.get('page')}]\n{node.get_content()[:500]}"
                )

        return "\n\n".join(sections)

    except Exception as e:
        logger.warning("Ошибка RAG-запроса: %s", e)
        return ""


def _get_index(manufacturer: str, model: str):
    """Получить или инициализировать LlamaIndex для модели оборудования."""
    cache_key = (manufacturer.lower(), model.lower())
    if cache_key in _index_cache:
        return _index_cache[cache_key]

    from llama_index.core import VectorStoreIndex
    from llama_index.core import Settings as LlamaSettings
    from llama_index.vector_stores.postgres import PGVectorStore
    from llama_index.embeddings.ollama import OllamaEmbedding

    from knowledge.indexer import _get_vector_store, _table_name

    LlamaSettings.embed_model = OllamaEmbedding(
        model_name=settings.embedding_model,
        base_url=settings.embedding_base_url,
        request_timeout=60.0,
    )
    LlamaSettings.llm = None

    vector_store = _get_vector_store(manufacturer, model)
    index = VectorStoreIndex.from_vector_store(vector_store)

    _index_cache[cache_key] = index
    logger.info("RAG-индекс загружен: %s/%s", manufacturer, model)
    return index


def invalidate_cache(manufacturer: str | None = None, model: str | None = None) -> None:
    """Сбросить кэш индексов после переиндексации."""
    if manufacturer and model:
        _index_cache.pop((manufacturer.lower(), model.lower()), None)
    else:
        _index_cache.clear()
