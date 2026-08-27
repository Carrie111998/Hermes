---
title: Исследование сосновой шишки — Агент RAG и долговременная память с сосновой
  шишкой
sidebar_label: Pinecone Research
description: Агент РАГ и долговременная память с шишкой
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Исследование сосновой шишки

Агент РАГ и долговременная память с Шишкой.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/pinecone-research` |
| Путь | `optional-skills/research/pinecone-research` |
| Версия | `1.0.0` |
| Автор | иммухаммадфуркан |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `pinecone-client`, `langchain-pinecone` |
| Платформы | Linux, MacOS, Windows |
| Теги | `RAG`, `Pinecone`, `Memory`, `Research`, `Vector Database`, `Agent`, `Retrieval` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Исследование сосновой шишки — агент RAG и долговременная память

Используйте Pinecone в качестве серверной части с расширенным поиском (RAG) для агента.
разговоры: сохранение вложений, извлечение соответствующего контекста из прошлого
занятия и развивают долговременную память.

## Когда использовать этот навык

**Используйте, когда:**
- Создание трубопроводов агента RAG с использованием сосновой шишки в качестве векторного хранилища.
- Требуется постоянная долговременная память во время сеансов агента.
- Сочетание поиска с использованием инструментов агента.
- Исследование или создание прототипов рабочих процессов семантического поиска.

**Вместо этого используйте навык «млопс/шишка», когда:**
- Нужен общий справочник по шишкам (управление индексами, CRUD, гибридный поиск).
- Работа на производственной инфраструктуре без интеграции агентов

## Быстрый старт

### Настройка

```bash
pip install pinecone-client langchain-pinecone langchain-openai
```

Установите свой ключ API:
```bash
export PINECONE_API_KEY="your-api-key"
```

### Базовый конвейер RAG

```python
from pinecone import Pinecone, ServerlessSpec
from langchain_pinecone import PineconeVectorStore
from langchain_openai import OpenAIEmbeddings

# Initialize Pinecone
pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])

# Create or connect to index
index_name = "agent-memory"
if index_name not in [i.name for i in pc.list_indexes()]:
    pc.create_index(
        name=index_name,
        dimension=1536,
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1"),
    )

# Build vector store
vectorstore = PineconeVectorStore.from_documents(
    documents=docs,
    embedding=OpenAIEmbeddings(),
    index_name=index_name,
)

# Retrieve relevant context
retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
results = retriever.invoke("What did the agent discuss yesterday?")
```

### Память сеанса на основе пространства имен

```python
# Store per-session memory
vectorstore = PineconeVectorStore(
    index=pc.Index(index_name),
    embedding=OpenAIEmbeddings(),
    namespace=f"session-{session_id}",
)

# Query across all sessions (no namespace filter)
all_memory = PineconeVectorStore(
    index=pc.Index(index_name),
    embedding=OpenAIEmbeddings(),
)
results = all_memory.similarity_search("relevant query", k=10)
```

## Лучшие практики

1. **Пространство имен по сеансу или пользователю** — изолируйте данные для мультитенантных агентов.
2. **Пакетное обновление** — 100–200 векторов на пакет для повышения эффективности.
3. **Фильтрация метаданных** — тегируйте векторы идентификатором сеанса, меткой времени и темой.
4. **Очистка старой памяти** — удаление устаревших пространств имен для контроля затрат.
5. **Используйте бессерверные решения** — автоматическое масштабирование, оплата по факту использования.

## Ресурсы

- **Документация по шишкам**: https://docs.pinecone.io.
- **Интеграция LangChain**: https://python.langchain.com/docs/integrations/vectorstores/pinecone
- **Уровень бесплатного пользования**: 1 индекс, 100 тыс. векторов (1536 измерений).