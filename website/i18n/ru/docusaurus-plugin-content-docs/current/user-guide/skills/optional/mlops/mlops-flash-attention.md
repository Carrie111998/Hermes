---
title: Flash Attention — Ускорьте обучение и вывод длинных последовательностей преобразователей.
sidebar_label: Flash Attention
description: Ускорьте обучение и вывод трансформаторов длинных последовательностей
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Вспышка внимания

Ускорьте обучение и вывод трансформаторов длинных последовательностей.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/mlops/flash-attention` |
| Путь | `optional-skills/mlops/flash-attention` |
| Версия | `1.0.1` |
| Автор | Исследование оркестра |
| Лицензия | Массачусетский технологический институт |
| Зависимости | `flash-attn`, `torch`, `transformers` |
| Платформы | Linux, MacOS |
| Теги | `Optimization`, `Flash Attention`, `Attention Optimization`, `Memory Efficiency`, `Speed Optimization`, `Long Context`, `PyTorch`, `SDPA`, `H100`, `FP8`, `Transformers` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Flash Attention — быстрое внимание с эффективным использованием памяти

## Быстрый старт

Flash Attention обеспечивает ускорение в 2–4 раза и сокращение памяти в 10–20 раз для повышения внимания трансформатора за счет разбивки и повторных вычислений с учетом операций ввода-вывода.

**Встроенная версия PyTorch (самая простая — PyTorch 2.2+)**:
```python
import torch
import torch.nn.functional as F

q = torch.randn(2, 8, 512, 64, device='cuda', dtype=torch.float16)  # [batch, heads, seq, dim]
k = torch.randn(2, 8, 512, 64, device='cuda', dtype=torch.float16)
v = torch.randn(2, 8, 512, 64, device='cuda', dtype=torch.float16)

# Automatically uses Flash Attention if available
out = F.scaled_dot_product_attention(q, k, v)
```

**библиотека flash-attn (дополнительные функции)**:
```bash
pip install flash-attn --no-build-isolation
```

```python
from flash_attn import flash_attn_func

# q, k, v: [batch, seqlen, nheads, headdim]
out = flash_attn_func(q, k, v, dropout_p=0.0, causal=True)
```

## Общие рабочие процессы

### Рабочий процесс 1: включить в существующей модели PyTorch

Скопируйте этот контрольный список:

```
Flash Attention Integration:
- [ ] Step 1: Check PyTorch version (≥2.2)
- [ ] Step 2: Enable Flash Attention backend
- [ ] Step 3: Verify speedup with profiling
- [ ] Step 4: Test accuracy matches baseline
```

**Шаг 1. Проверьте версию PyTorch**

```bash
python -c "import torch; print(torch.__version__)"
# Should be ≥2.2.0
```

Если &lt;2.2, обновить:
```bash
pip install --upgrade torch
```

**Шаг 2. Включите серверную часть Flash Attention**

Замените стандартное внимание:
```python
# Before (standard attention)
attn_weights = torch.softmax(q @ k.transpose(-2, -1) / math.sqrt(d_k), dim=-1)
out = attn_weights @ v

# After (Flash Attention)
import torch.nn.functional as F
out = F.scaled_dot_product_attention(q, k, v, attn_mask=mask)
```

Серверная часть Force Flash Attention (`torch.backends.cuda.sdp_kernel` устарела; используйте
`torch.nn.attention.sdpa_kernel` с `SDPBackend`):
```python
from torch.nn.attention import SDPBackend, sdpa_kernel

with sdpa_kernel(SDPBackend.FLASH_ATTENTION):
    out = F.scaled_dot_product_attention(q, k, v)
```

**Шаг 3. Проверьте ускорение с помощью профилирования**

```python
import torch.utils.benchmark as benchmark

def test_attention(use_flash):
    q, k, v = [torch.randn(2, 8, 2048, 64, device='cuda', dtype=torch.float16) for _ in range(3)]

    if use_flash:
        from torch.nn.attention import SDPBackend, sdpa_kernel
        with sdpa_kernel(SDPBackend.FLASH_ATTENTION):
            return F.scaled_dot_product_attention(q, k, v)
    else:
        attn = (q @ k.transpose(-2, -1) / 8.0).softmax(dim=-1)
        return attn @ v

# Benchmark
t_flash = benchmark.Timer(stmt='test_attention(True)', globals=globals())
t_standard = benchmark.Timer(stmt='test_attention(False)', globals=globals())

print(f"Flash: {t_flash.timeit(100).mean:.3f}s")
print(f"Standard: {t_standard.timeit(100).mean:.3f}s")
```

Ожидается: ускорение в 2–4 раза для последовательностей > 512 токенов.

**Шаг 4. Точность теста соответствует базовому уровню**

```python
# Compare outputs
q, k, v = [torch.randn(1, 8, 512, 64, device='cuda', dtype=torch.float16) for _ in range(3)]

# Flash Attention
out_flash = F.scaled_dot_product_attention(q, k, v)

# Standard attention
attn_weights = torch.softmax(q @ k.transpose(-2, -1) / 8.0, dim=-1)
out_standard = attn_weights @ v

# Check difference
diff = (out_flash - out_standard).abs().max()
print(f"Max difference: {diff:.6f}")
# Should be <1e-3 for float16
```

### Рабочий процесс 2: используйте библиотеку flash-attn для расширенных функций

Для множественного запроса, скользящего окна или H100 FP8.

Скопируйте этот контрольный список:

```
flash-attn Library Setup:
- [ ] Step 1: Install flash-attn library
- [ ] Step 2: Modify attention code
- [ ] Step 3: Enable advanced features
- [ ] Step 4: Benchmark performance
```

**Шаг 1. Установите библиотеку flash-attn**

```bash
# NVIDIA GPUs (CUDA 12.0+)
pip install flash-attn --no-build-isolation

# Verify installation
python -c "from flash_attn import flash_attn_func; print('Success')"
```

**Шаг 2. Измените код внимания**

```python
from flash_attn import flash_attn_func

# Input: [batch_size, seq_len, num_heads, head_dim]
# Transpose from [batch, heads, seq, dim] if needed
q = q.transpose(1, 2)  # [batch, seq, heads, dim]
k = k.transpose(1, 2)
v = v.transpose(1, 2)

out = flash_attn_func(
    q, k, v,
    dropout_p=0.1,
    causal=True,  # For autoregressive models
    window_size=(-1, -1),  # No sliding window
    softmax_scale=None  # Auto-scale
)

out = out.transpose(1, 2)  # Back to [batch, heads, seq, dim]
```

**Шаг 3. Включите расширенные функции**

Многозапросное внимание (разделение K/V по головкам):
```python
from flash_attn import flash_attn_func

# q: [batch, seq, num_q_heads, dim]
# k, v: [batch, seq, num_kv_heads, dim]  # Fewer KV heads
out = flash_attn_func(q, k, v)  # Automatically handles MQA
```

Скользящее окно внимания (локальное внимание):
```python
# Only attend to window of 256 tokens before/after
out = flash_attn_func(
    q, k, v,
    window_size=(256, 256),  # (left, right) window
    causal=True
)
```

**Шаг 4. Оценка производительности**

```python
import torch
from flash_attn import flash_attn_func
import time

q, k, v = [torch.randn(4, 4096, 32, 64, device='cuda', dtype=torch.float16) for _ in range(3)]

# Warmup
for _ in range(10):
    _ = flash_attn_func(q, k, v)

# Benchmark
torch.cuda.synchronize()
start = time.time()
for _ in range(100):
    out = flash_attn_func(q, k, v)
    torch.cuda.synchronize()
end = time.time()

print(f"Time per iteration: {(end-start)/100*1000:.2f}ms")
print(f"Memory allocated: {torch.cuda.max_memory_allocated()/1e9:.2f}GB")
```

### Рабочий процесс 3: оптимизация H100 FP8 (FlashAttention-3)

Для максимальной производительности на графических процессорах Hopper (H100).

> **Важно:** Пакет pip `flash-attn` (2.8.x) поставляется **только FlashAttention-2** — так и есть
> **не** содержит ядра FA3 или FP8 H100, а `flash_attn_func` **не** автоматически использует FP8.
> FlashAttention-3 — это отдельная **бета** сборка, скомпилированная из исходного кода репозитория `hopper/`.
> каталог, доступный через модуль `flash_attn_interface`. FA3 поддерживает FP16/BF16 вперед+назад.
> и **только вперед FP8**.

```
FP8 Setup:
- [ ] Step 1: Verify Hopper (H100) GPU available
- [ ] Step 2: Build & install FlashAttention-3 from source (hopper/)
- [ ] Step 3: Use the FA3 interface (FP8 forward)
```

**Шаг 1. Проверьте графический процессор H100**

```bash
nvidia-smi --query-gpu=name --format=csv
# Should show "H100" or "H800"
```

**Шаг 2. Сборка и установка FlashAttention-3 из исходного кода**

FA3 НЕ включен в `pip install flash-attn`. Соберите его из подкаталога `hopper/`:

```bash
git clone https://github.com/Dao-AILab/flash-attention.git
cd flash-attention/hopper
python setup.py install
# (compilation is heavy and requires a CUDA toolchain + Hopper GPU)
```

**Шаг 3. Используйте интерфейс FA3 (переход FP8)**

FA3 предоставляет собственный модуль `flash_attn_interface` (отличный от FA2 `flash_attn`).
FP8 — это путь **только вперед**, который ожидает входных данных `float8_e4m3fn`:

```python
import torch
from flash_attn_interface import flash_attn_func  # FA3 (hopper build), not `flash_attn`

# q, k, v: [batch, seqlen, nheads, headdim]
q = torch.randn(2, 4096, 32, 64, device='cuda', dtype=torch.float16)
k = torch.randn(2, 4096, 32, 64, device='cuda', dtype=torch.float16)
v = torch.randn(2, 4096, 32, 64, device='cuda', dtype=torch.float16)

# FP8 forward (inference / forward-only): cast to float8_e4m3fn
q_fp8 = q.to(torch.float8_e4m3fn)
k_fp8 = k.to(torch.float8_e4m3fn)
v_fp8 = v.to(torch.float8_e4m3fn)

out = flash_attn_func(q_fp8, k_fp8, v_fp8, causal=True)
# FP16/BF16 forward+backward is also supported by the FA3 interface.
```

## Когда использовать альтернативы

**Используйте Flash Attention, когда:**
- Тренировочные преобразователи с последовательностями >512 токенов
- Выполнение вывода с длинным контекстом (>2 000 токенов)
- Ограничение памяти графического процессора (OOM со стандартным вниманием)
- Требуется ускорение в 2-4 раза без потери точности.
- Использование PyTorch 2.2+ или установка flash-attn.

**Вместо этого используйте альтернативы:**
- **Стандартное внимание**: последовательности &lt;256 токенов (накладные расходы того не стоят)
- **xFormers**: нужно больше вариантов внимания (не только скорости).
- **Внимание с эффективным использованием памяти**: вывод ЦП (для Flash Attention требуется графический процессор)

## Распространенные проблемы

**Проблема: Ошибка импорта: невозможно импортировать flash_attn**

Установите с флагом no-build-isolation:
```bash
pip install flash-attn --no-build-isolation
```

Или сначала установите набор инструментов CUDA:
```bash
conda install cuda -c nvidia
pip install flash-attn --no-build-isolation
```

**Проблема: медленнее, чем ожидалось (без ускорения)**

Преимущества Flash Attention увеличиваются с увеличением длины последовательности:
- &lt;512 токенов: минимальное ускорение (10-20%)
- 512-2K токенов: ускорение в 2-3 раза
- >2 тыс. токенов: ускорение в 3-4 раза

Длина проверочной последовательности достаточна.

**Проблема: RuntimeError: ошибка CUDA**

Убедитесь, что графический процессор поддерживает Flash. Внимание:
```python
import torch
print(torch.cuda.get_device_capability())
# Should be ≥(7, 5) for Turing+
```

Вспышка внимания требует:
- Ампер (А100, А10): ✅ Полная поддержка
- Тьюринг (T4): ✅ Поддерживается
- Вольта (V100): ❌ Не поддерживается.

**Проблема: снижение точности**

Проверьте, что dtype имеет значение float16 или bfloat16 (не float32):
```python
q = q.to(torch.float16)  # Or torch.bfloat16
```

Flash Attention использует float16/bfloat16 для скорости. Float32 не поддерживается.

## Расширенные темы

**Интеграция с HuggingFace Transformers**: см. [references/transformers-integration.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/flash-attention/references/transformers-integration.md) для включения Flash Attention в моделях BERT, GPT, Llama.

**Бенчмарки производительности**: см. [references/benchmarks.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/flash-attention/references/benchmarks.md) для подробного сравнения скорости и памяти между графическими процессорами и длиной последовательности.

## Требования к оборудованию

- **Графический процессор**: NVIDIA Ampere+ (A100, A10, A30) или AMD MI200+.
- **VRAM**: то же, что и стандартное внимание (Flash Attention не увеличивает объем памяти).
- **CUDA**: 12,0+ (минимум 11,8)
- **PyTorch**: 2.2+ для встроенной поддержки.

**Не поддерживается**: V100 (Volta), определение ЦП

## Ресурсы

- Документ: «FlashAttention: быстрое и эффективное использование памяти точное внимание с учетом ввода-вывода» (NeurIPS 2022).
- Документ: «FlashAttention-2: более быстрое внимание с лучшим параллелизмом и разделением работы» (ICLR 2024).
- Блог: https://tridao.me/blog/2024/flash3/
- GitHub: https://github.com/Dao-AILab/flash-attention
- Документация PyTorch: https://pytorch.org/docs/stable/generated/torch.nn.functional.scaled_dot_product_attention.html.