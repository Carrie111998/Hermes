---
title: Huggingface Tokenizers — быстрая токенизация BPE/WordPiece и индивидуальное
  обучение словарному запасу.
sidebar_label: Huggingface Tokenizers
description: Быстрая токенизация BPE/WordPiece и индивидуальное обучение словарному
  запасу
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Токенизаторы Huggingface

Быстрая токенизация BPE/WordPiece и индивидуальное обучение словарному запасу.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/huggingface-tokenizers` |
| Путь | `optional-skills/mlops/huggingface-tokenizers` |
| Версия | `1.0.0` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `tokenizers`, `transformers`, `datasets` |
| Платформы | Linux, MacOS, Windows |
| Теги | `Tokenization`, `HuggingFace`, `BPE`, `WordPiece`, `Unigram`, `Fast Tokenization`, `Rust`, `Custom Tokenizer`, `Alignment Tracking`, `Production` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Токенизаторы HuggingFace — быстрая токенизация для НЛП

Быстрые, готовые к использованию токенизаторы с производительностью Rust и простотой использования Python.

## Когда использовать токенизаторы HuggingFace

**Используйте токенизаторы HuggingFace, когда:**
- Требуется чрезвычайно быстрая токенизация (&lt;20 секунд на ГБ текста)
- Обучение пользовательским токенизаторам с нуля.
- Хотите отслеживать выравнивание (токен → исходное положение текста)
- Построение производственных конвейеров НЛП
- Необходимость эффективной токенизации крупных корпораций

**Производительность**:
- **Скорость**: &lt;20 секунд для токенизации 1 ГБ на ЦП.
- **Реализация**: ядро Rust с привязками Python/Node.js.
- **Эффективность**: в 10–100 раз быстрее, чем реализации на чистом Python.

**Вместо этого используйте альтернативы**:
- **SentencePiece**: не зависит от языка, используется T5/ALBERT.
- **tiktoken**: токенизатор BPE OpenAI для моделей GPT.
- **Transformers AutoTokenizer**: загрузка только предварительно обученных (использует эту библиотеку внутри себя)

## Быстрый старт

### Установка

```bash
# Install tokenizers
pip install tokenizers

# With transformers integration
pip install tokenizers transformers
```

### Загрузка предварительно обученного токенизатора

```python
from tokenizers import Tokenizer

# Load from HuggingFace Hub
tokenizer = Tokenizer.from_pretrained("bert-base-uncased")

# Encode text
output = tokenizer.encode("Hello, how are you?")
print(output.tokens)  # ['hello', ',', 'how', 'are', 'you', '?']
print(output.ids)     # [7592, 1010, 2129, 2024, 2017, 1029]

# Decode back
text = tokenizer.decode(output.ids)
print(text)  # "hello, how are you?"
```

### Обучение пользовательскому токенизатору BPE

```python
from tokenizers import Tokenizer
from tokenizers.models import BPE
from tokenizers.trainers import BpeTrainer
from tokenizers.pre_tokenizers import Whitespace

# Initialize tokenizer with BPE model
tokenizer = Tokenizer(BPE(unk_token="[UNK]"))
tokenizer.pre_tokenizer = Whitespace()

# Configure trainer
trainer = BpeTrainer(
    vocab_size=30000,
    special_tokens=["[UNK]", "[CLS]", "[SEP]", "[PAD]", "[MASK]"],
    min_frequency=2
)

# Train on files
files = ["train.txt", "validation.txt"]
tokenizer.train(files, trainer)

# Save
tokenizer.save("my-tokenizer.json")
```

**Время обучения**: ~1–2 минуты для корпуса 100 МБ, ~10–20 минут для 1 ГБ

### Пакетное кодирование с заполнением

```python
# Enable padding
tokenizer.enable_padding(pad_id=3, pad_token="[PAD]")

# Encode batch
texts = ["Hello world", "This is a longer sentence"]
encodings = tokenizer.encode_batch(texts)

for encoding in encodings:
    print(encoding.ids)
# [101, 7592, 2088, 102, 3, 3, 3]
# [101, 2023, 2003, 1037, 2936, 6251, 102]
```

## Алгоритмы токенизации

### BPE (кодирование парами байтов)

**Как это работает**:
1. Начните со словарного запаса на уровне персонажа.
2. Найдите наиболее часто встречающуюся пару символов.
3. Объединить в новый токен, добавить в словарь
4. Повторяйте до тех пор, пока не будет достигнут размер словарного запаса.

**Используется**: GPT-2, GPT-3, RoBERTa, BART, DeBERTa.

```python
from tokenizers import Tokenizer
from tokenizers.models import BPE
from tokenizers.trainers import BpeTrainer
from tokenizers.pre_tokenizers import ByteLevel

tokenizer = Tokenizer(BPE(unk_token="<|endoftext|>"))
tokenizer.pre_tokenizer = ByteLevel()

trainer = BpeTrainer(
    vocab_size=50257,
    special_tokens=["<|endoftext|>"],
    min_frequency=2
)

tokenizer.train(files=["data.txt"], trainer=trainer)
```

**Преимущества**:
- Хорошо обрабатывает OOV-слова (разбивается на подслова)
- Гибкий размер словарного запаса
- Подходит для морфологически богатых языков.

**Компромиссы**:
- Токенизация зависит от порядка слияния
- Может неожиданно разделить общие слова

### Слово

**Как это работает**:
1. Начните со словарного запаса персонажей
2. Оценка пар слияния: `frequency(pair) / (frequency(first) × frequency(second))`
3. Объедините пару с самым высоким результатом.
4. Повторяйте до тех пор, пока не будет достигнут размер словарного запаса.

**Используется**: BERT, DistilBERT, MobileBERT

```python
from tokenizers import Tokenizer
from tokenizers.models import WordPiece
from tokenizers.trainers import WordPieceTrainer
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.normalizers import BertNormalizer

tokenizer = Tokenizer(WordPiece(unk_token="[UNK]"))
tokenizer.normalizer = BertNormalizer(lowercase=True)
tokenizer.pre_tokenizer = Whitespace()

trainer = WordPieceTrainer(
    vocab_size=30522,
    special_tokens=["[UNK]", "[CLS]", "[SEP]", "[PAD]", "[MASK]"],
    continuing_subword_prefix="##"
)

tokenizer.train(files=["corpus.txt"], trainer=trainer)
```

**Преимущества**:
- Отдает приоритет значимым слияниям (высокий балл = семантически связаны)
- Успешно используется в BERT (самые современные результаты)

**Компромиссы**:
- Неизвестные слова становятся `[UNK]`, если ни одно подслово не соответствует.
- Сохраняет словарный запас, а не объединяет правила (файлы большего размера)

### Униграмма

**Как это работает**:
1. Начните с большого словарного запаса (все подстроки)
2. Вычислить потери для корпуса с текущим словарным запасом.
3. Удаление токенов с минимальным влиянием на потерю
4. Повторяйте до тех пор, пока не будет достигнут размер словарного запаса.

**Используется**: ALBERT, T5, mBART, XLNet (через SentencePiece)

```python
from tokenizers import Tokenizer
from tokenizers.models import Unigram
from tokenizers.trainers import UnigramTrainer

tokenizer = Tokenizer(Unigram())

trainer = UnigramTrainer(
    vocab_size=8000,
    special_tokens=["<unk>", "<s>", "</s>"],
    unk_token="<unk>"
)

tokenizer.train(files=["data.txt"], trainer=trainer)
```

**Преимущества**:
- Вероятностный (находит наиболее вероятную токенизацию)
- Хорошо работает для языков без границ слов.
- Работает с различными языковыми контекстами.

**Компромиссы**:
- Вычислительно дорогое обучение
- Больше гиперпараметров для настройки

## Конвейер токенизации

Полный конвейер: **Нормализация → Предварительная токенизация → Модель → Постобработка**

### Нормализация

Очистите и стандартизируйте текст:

```python
from tokenizers.normalizers import NFD, StripAccents, Lowercase, Sequence

tokenizer.normalizer = Sequence([
    NFD(),           # Unicode normalization (decompose)
    Lowercase(),     # Convert to lowercase
    StripAccents()   # Remove accents
])

# Input: "Héllo WORLD"
# After normalization: "hello world"
```

**Общие нормализаторы**:
- `NFD`, `NFC`, `NFKD`, `NFKC` — формы нормализации Unicode
- `Lowercase()` - Преобразовать в нижний регистр
- `StripAccents()` - Удалить акценты (é → e)
- `Strip()` - Удалить пробелы
- `Replace(pattern, content)` - замена регулярных выражений

### Предварительная токенизация

Разделите текст на словесные единицы:

```python
from tokenizers.pre_tokenizers import Whitespace, Punctuation, Sequence, ByteLevel

# Split on whitespace and punctuation
tokenizer.pre_tokenizer = Sequence([
    Whitespace(),
    Punctuation()
])

# Input: "Hello, world!"
# After pre-tokenization: ["Hello", ",", "world", "!"]
```

**Общие предварительные токенизаторы**:
- `Whitespace()` - Разделение по пробелам, табуляции, новой строке
- `ByteLevel()` - разделение на уровне байтов в стиле GPT-2.
- `Punctuation()` - Изолировать знаки препинания
- `Digits(individual_digits=True)` — Разделить цифры по отдельности
- `Metaspace()` - Заменить пробелы на (стиль SentencePiece)

### Постобработка

Добавьте специальные токены для ввода модели:

```python
from tokenizers.processors import TemplateProcessing

# BERT-style: [CLS] sentence [SEP]
tokenizer.post_processor = TemplateProcessing(
    single="[CLS] $A [SEP]",
    pair="[CLS] $A [SEP] $B [SEP]",
    special_tokens=[
        ("[CLS]", 1),
        ("[SEP]", 2),
    ],
)
```

**Общие шаблоны**:
```python
# GPT-2: sentence <|endoftext|>
TemplateProcessing(
    single="$A <|endoftext|>",
    special_tokens=[("<|endoftext|>", 50256)]
)

# RoBERTa: <s> sentence </s>
TemplateProcessing(
    single="<s> $A </s>",
    pair="<s> $A </s> </s> $B </s>",
    special_tokens=[("<s>", 0), ("</s>", 2)]
)
```

## Отслеживание выравнивания

Отслеживать позиции токенов в исходном тексте:

```python
output = tokenizer.encode("Hello, world!")

# Get token offsets
for token, offset in zip(output.tokens, output.offsets):
    start, end = offset
    print(f"{token:10} → [{start:2}, {end:2}): {text[start:end]!r}")

# Output:
# hello      → [ 0,  5): 'Hello'
# ,          → [ 5,  6): ','
# world      → [ 7, 12): 'world'
# !          → [12, 13): '!'
```

**Случаи использования**:
- Распознавание названного объекта (предсказания карты возвращаются в текст)
- Ответы на вопросы (извлечение интервалов ответов)
- Классификация токенов (выровнять метки по исходным позициям)

## Интеграция с трансформерами

### Загрузка с помощью AutoTokenizer

```python
from transformers import AutoTokenizer

# AutoTokenizer automatically uses fast tokenizers
tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")

# Check if using fast tokenizer
print(tokenizer.is_fast)  # True

# Access underlying tokenizers.Tokenizer
fast_tokenizer = tokenizer.backend_tokenizer
print(type(fast_tokenizer))  # <class 'tokenizers.Tokenizer'>
```

### Преобразование пользовательского токенизатора в преобразователи

```python
from tokenizers import Tokenizer
from transformers import PreTrainedTokenizerFast

# Train custom tokenizer
tokenizer = Tokenizer(BPE())
# ... train tokenizer ...
tokenizer.save("my-tokenizer.json")

# Wrap for transformers
transformers_tokenizer = PreTrainedTokenizerFast(
    tokenizer_file="my-tokenizer.json",
    unk_token="[UNK]",
    pad_token="[PAD]",
    cls_token="[CLS]",
    sep_token="[SEP]",
    mask_token="[MASK]"
)

# Use like any transformers tokenizer
outputs = transformers_tokenizer(
    "Hello world",
    padding=True,
    truncation=True,
    max_length=512,
    return_tensors="pt"
)
```

## Общие шаблоны

### Обучение из итератора (большие наборы данных)

```python
from datasets import load_dataset

# Load dataset
dataset = load_dataset("wikitext", "wikitext-103-raw-v1", split="train")

# Create batch iterator
def batch_iterator(batch_size=1000):
    for i in range(0, len(dataset), batch_size):
        yield dataset[i:i + batch_size]["text"]

# Train tokenizer
tokenizer.train_from_iterator(
    batch_iterator(),
    trainer=trainer,
    length=len(dataset)  # For progress bar
)
```

**Производительность**: обработка 1 ГБ примерно за 10–20 минут.

### Включить усечение и дополнение

```python
# Enable truncation
tokenizer.enable_truncation(max_length=512)

# Enable padding
tokenizer.enable_padding(
    pad_id=tokenizer.token_to_id("[PAD]"),
    pad_token="[PAD]",
    length=512  # Fixed length, or None for batch max
)

# Encode with both
output = tokenizer.encode("This is a long sentence that will be truncated...")
print(len(output.ids))  # 512
```

### Многопроцессорность

```python
from tokenizers import Tokenizer
from multiprocessing import Pool

# Load tokenizer
tokenizer = Tokenizer.from_file("tokenizer.json")

def encode_batch(texts):
    return tokenizer.encode_batch(texts)

# Process large corpus in parallel
with Pool(8) as pool:
    # Split corpus into chunks
    chunk_size = 1000
    chunks = [corpus[i:i+chunk_size] for i in range(0, len(corpus), chunk_size)]

    # Encode in parallel
    results = pool.map(encode_batch, chunks)
```

**Ускорение**: 5–8 раз при 8 ядрах

## Тесты производительности

### Скорость тренировки

| Размер корпуса | BPE (30 тысяч слов) | WordPiece (30 КБ) | Униграмма (8k) |
|-------------|-----------------|-----------------|--------------|
| 10 МБ | 15 секунд | 18 сек | 25 секунд |
| 100 МБ | 1,5 мин | 2 мин | 4 мин |
| 1 ГБ | 15 мин | 20 мин | 40 мин |

**Аппаратное обеспечение**: 16-ядерный процессор, протестировано в английской Википедии.

### Скорость токенизации

| Реализация | Корпус 1 ГБ | Пропускная способность |
|----------------|-------------|---------------|
| Чистый Питон | ~20 минут | ~50 МБ/мин |
| HF-токенизаторы | ~15 секунд | ~4 ГБ/мин |
| **Ускорение** | **80×** | **80×** |

**Тест**: текст на английском языке, средняя длина предложения 20 слов.

### Использование памяти

| Задача | Память |
|-------------------------|---------|
| Загрузить токенизатор | ~10 МБ |
| Поезд БПЭ (30к словарного запаса) | ~200 МБ |
| Закодируйте 1 млн предложений | ~500 МБ |

## Поддерживаемые модели

Предварительно обученные токенизаторы доступны через `from_pretrained()`:

**Семья БЕРТ**:
- `bert-base-uncased`, `bert-large-cased`
- `distilbert-base-uncased`
- `roberta-base`, `roberta-large`

**Семейство GPT**:
- `gpt2`, `gpt2-medium`, `gpt2-large`
- `distilgpt2`

**Семейство T5**:
- `t5-small`, `t5-base`, `t5-large`
- `google/flan-t5-xxl`

**Другое**:
- `facebook/bart-base`, `facebook/mbart-large-cc25`
- `albert-base-v2`, `albert-xlarge-v2`
- `xlm-roberta-base`, `xlm-roberta-large`

Посмотреть все: https://huggingface.co/models?library=tokenizers

## Ссылки

- **[Руководство по обучению](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/huggingface-tokenizers/references/training.md)** - Обучение пользовательским токенизаторам, настройка тренеров, обработка больших наборов данных
- **[Подробное описание алгоритмов](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/huggingface-tokenizers/references/algorithms.md)** - Подробное объяснение BPE, WordPiece, Unigram
- **[Компоненты конвейера](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/huggingface-tokenizers/references/pipeline.md)** - Нормализаторы, пре-токенизаторы, постпроцессоры, декодеры
- **[Интеграция трансформеров](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/huggingface-tokenizers/references/integration.md)** - AutoTokenizer, PreTrainedTokenizerFast, специальные токены

## Ресурсы

- **Документация**: https://huggingface.co/docs/tokenizers.
- **GitHub**: https://github.com/huggingface/tokenizers ⭐ 9000+
- **Версия**: 0.20.0+
- **Курс**: https://huggingface.co/learn/nlp-course/chapter6/1
- **Документ**: BPE (Sennrich et al., 2016), WordPiece (Schuster & Nakajima, 2012).