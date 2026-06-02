"""RAG-запросы к pgvector-индексу: поиск релевантных описаний регистров и fault-кодов."""
import html
import logging
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# Кэш инициализированных индексов
_index_cache: dict[str, Any] = {}


def retrieve_context(
    kb_path: str,
    active_addrs: list[int],
    fault_addrs: list[int] | None = None,
    top_k: int = 20,
) -> str:
    """Извлечь релевантный контекст из knowledge base для промпта агента.

    Формирует запрос из активных адресов регистров и fault-кодов,
    возвращает текстовый блок для вставки в system prompt.
    """
    if not kb_path or not active_addrs:
        return ""

    try:
        index = _get_index(kb_path)
    except Exception as e:
        logger.warning("RAG недоступен для %s: %s", kb_path, e)
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
            content = html.unescape(node.get_content())

            if doc_type == "register":
                sections.append(f"[Регистр {meta.get('addr')}] {content}")
            elif doc_type == "fault":
                sections.append(
                    f"[Fault addr={meta.get('addr')} bit={meta.get('bit')} "
                    f"severity={meta.get('severity')}] {content}"
                )
            elif doc_type == "manual":
                sections.append(
                    f"[РЭ {meta.get('source')} стр.{meta.get('page')}]\n{content[:500]}"
                )

        return "\n\n".join(sections)

    except Exception as e:
        logger.warning("Ошибка RAG-запроса: %s", e)
        return ""


def _get_index(kb_path: str):
    """Получить или инициализировать LlamaIndex для папки kb_path."""
    cache_key = kb_path.lower()
    if cache_key in _index_cache:
        return _index_cache[cache_key]

    from llama_index.core import VectorStoreIndex
    from llama_index.core import Settings as LlamaSettings
    from llama_index.vector_stores.postgres import PGVectorStore
    from llama_index.embeddings.ollama import OllamaEmbedding

    from knowledge.indexer import _get_vector_store

    LlamaSettings.embed_model = OllamaEmbedding(
        model_name=settings.embedding_model,
        base_url=settings.embedding_base_url,
        request_timeout=60.0,
    )
    LlamaSettings.llm = None

    vector_store = _get_vector_store(kb_path)
    index = VectorStoreIndex.from_vector_store(vector_store)

    _index_cache[cache_key] = index
    logger.info("RAG-индекс загружен: %s", kb_path)
    return index


def invalidate_cache(kb_path: str | None = None) -> None:
    """Сбросить кэш индексов после переиндексации."""
    if kb_path:
        _index_cache.pop(kb_path.lower(), None)
    else:
        _index_cache.clear()


def search_manual_docs(query: str, kb_path: str, top_k: int = 4) -> str:
    """Семантический поиск по PDF-документации оборудования.

    В отличие от retrieve_context(), формирует осмысленный текстовый запрос
    и возвращает только чанки типа 'manual' (из PDF).
    Используется инструментом search_manual в corpus/executor.py.
    """
    if not query or not kb_path:
        return ""

    try:
        index = _get_index(kb_path)
    except Exception as e:
        logger.warning("search_manual_docs: индекс недоступен для %s: %s", kb_path, e)
        return ""

    try:
        retriever = index.as_retriever(similarity_top_k=top_k * 3)  # берём с запасом, потом фильтруем
        nodes = retriever.retrieve(query)

        if not nodes:
            return ""

        sections = []
        for node in nodes:
            meta = node.metadata or {}
            # Только документация (PDF-чанки), не регистры и не fault-биты
            if meta.get("type") != "manual":
                continue
            content = html.unescape(node.get_content()).strip()
            if not content:
                continue
            source = meta.get("source", "")
            page = meta.get("page", "")
            sections.append(f"[{source}, стр. {page}]\n{content}")
            if len(sections) >= top_k:
                break

        return "\n\n---\n\n".join(sections) if sections else ""

    except Exception as e:
        logger.warning("search_manual_docs ошибка запроса: %s", e)
        return ""
