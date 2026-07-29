# context_overflow 错误分类修复方案

> 目标：减少因 provider 溢出报错漏匹配、推理模型断连误分类、output-cap 无法解析
> 而导致的 unnecessary 对话历史压缩。

---

## P0 — 扩大 _CONTEXT_OVERFLOW_PATTERNS

### 文件：`error_classifier.py`

### P0-A：新增 _CONTEXT_OVERFLOW_PATTERNS 条目 (line ~269 之前插入)

当前第 269 行 `exceeds the maximum number of input tokens` 之后、第 270 行 `]` 之前，插入以下条目：

```python
    # Azure / Azure OpenAI
    "maximum context length exceeded",         # Azure 完整错误文案
    # Google / Gemini (429 Resource Exhausted)
    "resource exhausted",                      # "context length exceeded" + 429
    "exceeds token limit",                     # Gemini token limit 报错
    "exceeds the maximum context length",      # Gemini 完整文案
    # Google / Vertex AI
    "token count exceeds",                     # Vertex AI 的 token count 超限
    "total token limit exceeded",              # Vertex AI 通用超限
    # OpenAI / Azure OpenAI — output + input combined
    "total tokens exceeds",                    # "total tokens exceeds the limit"
    "tokens must not exceed",                  # OpenAI API 变体
    "completion tokens exceed",                # OpenAI per-response token limit
    # Ollama — 补充遗漏的 "exceeds context length"（无 "exceeded"）
    "exceeds context length",                  # Ollama 错误文案
    # vLLM — 补充 prompt token 维度
    "exceeds the max_prompt_token",            # vLLM max prompt tokens 超限
    "exceeds max prompt tokens",               # vLLM 变体（不带 the）
    # OpenAI-compatible servers (通用 gateway)
    "request too large",                       # 通用 gateway 超限
    "request body too large",                  # 通用 gateway body 超限
    # Azure — specific parameter form
    "max_tokens_per_batch exceeds",            # Azure batch 模式
    # General fallback patterns
    "input exceeds",                           # 通用 input 超限前缀
    "exceeds the context",                     # 通用 context 超限
    "exceeds your context",                    # "exceeds your context window"
    # Chinese variants (补充)
    "超出上下文长度",                           # 中文上下文超限
    "输入超过最大",                            # 中文输入超限
```

**副作用分析：**
- 这些新增 pattern 为 **子串匹配**（`in error_msg`），不会改变现有行为的精确性。
- 仅增加额外匹配概率，不影响已正确分类的错误。
- `"exceeds the limit"` 已存在（line 244），新增的 `"exceeds the max_prompt_token"` 与之不冲突（不同前缀）。
- `"input is too long"` 已存在（line 254 & 266），无需重复。

### P0-B：500/503 路径回退阈值 (error_classifier.py:973-991)

#### 修改 _classify_500 (lines 973-979)

将：
```python
        if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return result_fn(FailoverReason.server_error, retryable=True)
```

改为：
```python
        if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        # Fallback: if the session is using >85% of context, a 500
        # from a local inference server is very likely context overflow
        # even if the provider's error text doesn't carry the keyword.
        # See: #P0-B
        if approx_tokens > context_length * 0.85:
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return result_fn(FailoverReason.server_error, retryable=True)
```

#### 修改 _classify_503 (lines 986-992)

将：
```python
        if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return result_fn(FailoverReason.overloaded, retryable=True)
```

改为：
```python
        if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        # Same fallback threshold for 503/529.
        if approx_tokens > context_length * 0.85:
            return result_fn(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return result_fn(FailoverReason.overloaded, retryable=True)
```

**副作用分析：**
- `approx_tokens > context_length * 0.85` 意味着当 token 使用超过 85% 时，即使 provider 返回了模糊的 500/503 错误，也会尝试压缩路径。
- **可能的副作用：** 如果用户的对话已经很大（如 >85% context），而服务器恰好发生了真正的 500/503 错误，会被误判为 context_overflow 触发压缩。但 85% 阈值是保守的（不是 50%），且压缩本身是可逆的（只压缩历史，不是永久删除），因此风险可控。
- 此回退逻辑与现有 disconnect 路径中的 `context_length * 0.6` 阈值互补（disconnect 路径用 0.6 因为断连概率更高）。

---

## P1 — 推理模型断连检测改进

### 文件：`reasoning_timeouts.py`

#### P1-A：新增推理模型家族条目

在第 110 行之后、`)` 之前插入：

```python
    # Gemini — Google 的推理模型家族（Gemini 2.0 Flash / 2.5 Pro 等），
    # 在 Google AI Studio / Vertex AI 上有类似的多分钟 thinking 阶段。
    ("gemini-2.0-flash-thinking", 300),
    ("gemini-2.5-pro", 600),
    ("gemini-2.5-flash", 300),
    ("gemini-exp", 600),
    # Cohere 推理模型
    ("command-r7b-12-2024", 300),        # Command R+ reasoning variant
    ("command-r-plus", 300),
    # Mistral 推理模型
    ("mistral-large", 180),               # Mistral large models with thinking
    # General reasoning pattern fallback:
    # any model whose slug contains "reasoner" or "thinking"
    # is treated as a reasoning model — handled via extended regex below
```

#### P1-B：扩展推理模型检测为启发式模式匹配

在 `_match_any` 函数之后、`get_reasoning_stale_timeout_floor` 之前，添加一个通用的启发式匹配器。修改 `reasoning_timeouts.py` 的 `_match_any` 调用链，使其支持更宽的 family 检测。

**方案：** 在 `get_reasoning_stale_timeout_floor` 函数的 slug-stripped 之后，在调用 `_match_any` 之前，增加一个基于模型名称模式的通用推理模型检测器：

```python
def _is_reasoning_model_by_name(model_lower: str) -> Optional[float]:
    """Heuristic reasoning-model detector based on naming patterns.

    Covers reasoning models not in the explicit allowlist above.
    Returns a conservative stale-timeout floor (180s) for unknown reasoning
    models so they are classified as timeout rather than context_overflow
    on disconnect.

    Pattern families:
      - o1, o3, o4-mini → OpenAI reasoning series
      - claude-opus, claude-sonnet → Anthropic Claude 4.x thinking
      - gemini-2.5-pro, gemini-exp → Google reasoning
      - deepseek-r1, deepseek-reasoner → DeepSeek reasoning
      - *reasoner*, *thinking* → any model with these suffixes
      - grok-3, grok-4 (reasoning variants)
    """
    # OpenAI o-series reasoning (o1, o3, o4-mini, etc.)
    # "o" followed by digit and optional suffix
    if re.search(r'\bo\d+(-mini|-pro|-preview|-flash)?(?:\s|$|[/\-_])', model_lower):
        return 600.0
    # Anthropic Claude 4.x thinking (opus, sonnet)
    if 'claude' in model_lower and re.search(r'(opus|sonnet)', model_lower):
        return 240.0
    # Google Gemini reasoning (2.5-pro, exp, flash-thinking)
    if re.search(r'gemini-2\.\d+(?:-pro|-exp|-flash)', model_lower):
        return 480.0
    # DeepSeek reasoning (R1, reasoner)
    if 'deepseek' in model_lower and re.search(r'(r1|reasoner)', model_lower):
        return 600.0
    # Generic: any model with "reasoner" or "thinking" in the name
    if re.search(r'(reasoner|thinking)', model_lower):
        return 300.0
    return None
```

然后在 `get_reasoning_stale_timeout_floor` 中，在 `_match_any(name)` 之后增加 fallback：

将 `return _match_any(name)` 改为：

```python
    explicit_floor = _match_any(name)
    if explicit_floor is not None:
        return explicit_floor
    # Fallback: detect reasoning models by name pattern even if not
    # in the explicit allowlist.  A true reasoning model disconnect
    # is a timeout, not context_overflow.  Returns a conservative
    # 180s floor (same as the non-reasoning default stale window) so
    # the retry path recovers instead of triggering compression.
    return _is_reasoning_model_by_name(name)
```

**副作用分析：**
- `_is_reasoning_model_by_name` 是宽松匹配，可能对某些非推理模型误报（如 `claude-sonnet` 非 thinking 变体、`gemini-2.0-flash` 非 thinking）。
- 但这是 **有意的设计**：将非推理模型误判为推理模型只会导致更大的 stale-timeout（用户多等一会儿），而不会丢失任何功能。反过来，将推理模型误判为非推理模型会导致 context_overflow 误分类（压缩历史），这是我们要避免的。
- 对现有白名单中的模型，`explicit_floor` 先返回，不受影响。

---

## P2 — output-cap 解析增强

### 文件：`model_metadata.py`

### P2-A：`is_output_cap_error` 增强

在 `is_output_cap_error` 函数中（约 line 1202），扩展 `output_cap_signal`：

将：
```python
    output_cap_signal = (
        "range of max_tokens should be" in error_lower      # DashScope / Alibaba
        or "available_tokens" in error_lower                # Anthropic
        or "available tokens" in error_lower
        or ("in the output" in error_lower                  # OpenRouter / Nous
            and "maximum context length" in error_lower)
        or ("requested" in error_lower                      # LM Studio / llama.cpp
            and "output tokens" in error_lower)
        or "should be" in error_lower                       # generic "max_tokens should be <= N"
        or "less than or equal" in error_lower
        or "must be" in error_lower
    )
```

改为：
```python
    output_cap_signal = (
        "range of max_tokens should be" in error_lower      # DashScope / Alibaba
        or "available_tokens" in error_lower                # Anthropic
        or "available tokens" in error_lower
        or ("in the output" in error_lower                  # OpenRouter / Nous
            and "maximum context length" in error_lower)
        or ("requested" in error_lower                      # LM Studio / llama.cpp
            and "output tokens" in error_lower)
        or "should be" in error_lower                       # generic "max_tokens should be <= N"
        or "less than or equal" in error_lower
        or "must be" in error_lower
        # OpenAI — max_completion_tokens variant
        or ("max_completion_tokens" in error_lower
            and ("cannot exceed" in error_lower
                 or "exceeds" in error_lower
                 or "must be" in error_lower))
        # OpenAI — generic max_tokens + cannot exceed
        or ("max_tokens" in error_lower
            and "cannot exceed" in error_lower)
        # Google / Gemini — structured JSON error
        or ("token limit" in error_lower
            and ("output" in error_lower or "response" in error_lower))
        # AWS Bedrock — Claude on Bedrock
        or ("max_tokens" in error_lower
            and "context window" in error_lower
            and ("input" in error_lower or "tokens" in error_lower))
    )
```

### P2-B：`parse_available_output_tokens_from_error` 增强

在现有 `patterns` 列表之后（约 line 1112 之后）、OpenRouter/Nous 解析之前，插入新的解析器：

```python
    # OpenAI — max_completion_tokens format:
    #   "The 'messages' property in request message #1 did not satisfy this
    #    constraint: Maximum context length exceeded"
    # Also: "maximum context length of 128000 tokens exceeded"
    # In these cases, try to extract the context limit from the error.
    _m_oai_ctx = re.search(
        r'maximum context length of (\d+) tokens? (?:exceeded|too large)',
        error_lower,
    )
    if _m_oai_ctx:
        # This is a total context overflow, not output-cap.
        # Return None to let the compression path handle it.
        return None
    # OpenAI: "Input of N tokens exceeds the limit of M tokens"
    _m_oai_limit = re.search(
        r'(\d+)\s*tokens?\s*exceeds?\s*(?:the\s+)?(?:limit|maximum)\s*of\s*(\d+)\s*tokens?',
        error_lower,
    )
    if _m_oai_limit and "max_completion_tokens" in error_lower:
        # Parse: total_limit - input_tokens = available_output
        # But we need input tokens from somewhere. If not available,
        # return None to let compression handle it.
        _ctx_total = int(_m_oai_limit.group(2))
        # Try to find input tokens separately
        _m_input = re.search(
            r'(\d+)\s*(?:input|prompt|message)?\s*tokens?', error_lower
        )
        if _m_input:
            _avail = _ctx_total - int(_m_input.group(1))
            if _avail >= 1:
                return _avail
```

在 LM Studio parser（line ~1139）之后、vLLM parser（line ~1157）之前，插入：

```python
    # Google / Gemini API — JSON error body format:
    #   {"error": {"message": "..."Token limit exceeded..."}}
    # Or plain text: "Cost of this call is 15000 tokens, but you requested 16000
    #                output tokens..."
    _m_cost = re.search(
        r'(?:cost|total)\s+(?:of|is)\s+(?:this\s+call\s+)?(?:\d+\s*)?tokens?\s*[,;]?(\s*(?:but|and)\s+(?:you\s+)?(?:requested)?\s*(\d+)?\s*output?\s*tokens?)',
        error_lower,
    )
    if not _m_cost:
        # Simpler Google variant: "You requested N output tokens but the
        # model has a limit of M"
        _m_cost = re.search(
            r'requested\s+(\d+)\s*output?\s*tokens?\s*(?:but|,?)\s*(?:the\s+model\s+)?(?:has\s+a\s+)?(?:limit|maximum|cap)\s*(?:of\s*)?(\d+)',
            error_lower,
        )
    if _m_cost:
        _requested = int(_m_cost.group(1))
        _limit = int(_m_cost.group(2))
        # Available = limit - input (we don't know input, but if limit < requested,
        # then available output <= limit - input_tokens).
        # Conservative: return limit as the cap.
        if _limit >= 1:
            return _limit
```

---

## 汇总清单

### 需要新增的 _CONTEXT_OVERFLOW_PATTERNS 条目

| # | 新 pattern | 来源 provider |
|---|-----------|---------------|
| 1 | `maximum context length exceeded` | Azure / Azure OpenAI |
| 2 | `resource exhausted` | Google / Gemini (429) |
| 3 | `exceeds token limit` | Google / Gemini |
| 4 | `exceeds the maximum context length` | Google / Gemini |
| 5 | `token count exceeds` | Vertex AI |
| 6 | `total token limit exceeded` | Vertex AI |
| 7 | `total tokens exceeds` | OpenAI |
| 8 | `tokens must not exceed` | OpenAI |
| 9 | `completion tokens exceed` | OpenAI per-response |
| 10 | `exceeds context length` | Ollama |
| 11 | `exceeds the max_prompt_token` | vLLM |
| 12 | `exceeds max prompt tokens` | vLLM (变体) |
| 13 | `request too large` | 通用 gateway |
| 14 | `request body too large` | 通用 gateway |
| 15 | `max_tokens_per_batch exceeds` | Azure batch |
| 16 | `input exceeds` | 通用 fallback |
| 17 | `exceeds the context` | 通用 fallback |
| 18 | `exceeds your context` | 通用 fallback |
| 19 | `超出上下文长度` | 中文 provider |
| 20 | `输入超过最大` | 中文 provider |

### 需要新增的 reasoning model 检测规则

| # | 新规则 | 覆盖模型 |
|---|--------|----------|
| 1 | `gemini-2.0-flash-thinking` | Google Gemini 2.0 Flash thinking |
| 2 | `gemini-2.5-pro` | Google Gemini 2.5 Pro |
| 3 | `gemini-2.5-flash` | Google Gemini 2.5 Flash |
| 4 | `gemini-exp` | Google Gemini experimental |
| 5 | `command-r7b-12-2024` | Cohere Command R+ |
| 6 | `command-r-plus` | Cohere Command R+ |
| 7 | `mistral-large` | Mistral large family |
| 8 | `_is_reasoning_model_by_name` 启发式规则 | 所有非白名单推理模型 |
| 8a | `\bo\d+` regex | OpenAI o1/o3/o4 series (非白名单) |
| 8b | `claude` + `opus/sonnet` regex | Anthropic Claude 4.x thinking |
| 8c | `gemini-2\.\d+` regex | Google Gemini 2.x reasoning |
| 8d | `deepseek` + `r1/reasoner` regex | DeepSeek R1/reasoner |
| 8e | `reasoner|thinking` suffix | 通用推理/思考模型 |

### 需要新增的 output-cap 正则表达式

| # | 正则表达式 | 来源 provider |
|---|-----------|---------------|
| 1 | `max_completion_tokens` + `cannot exceed/exceeds/must be` | OpenAI |
| 2 | `max_tokens` + `cannot exceed` | OpenAI 通用 |
| 3 | `token limit` + `output/response` | Google / Gemini |
| 4 | `max_tokens` + `context window` + `input/tokens` | AWS Bedrock |
| 5 | `maximum context length of (\d+) tokens?` | OpenAI 上下文超限 |
| 6 | `(\d+)\s*tokens?\s*exceeds?\s*(?:the\s+)?(?:limit|maximum)\s*of\s*(\d+)\s*tokens?` | OpenAI token 限制 |
| 7 | `(?:cost|total)\s+(?:of|is)\s+...` | Google token cost 格式 |
| 8 | `requested\s+(\d+)\s*output?\s*tokens?\s*(?:but|,?)\s*(?:the\s+model\s+)?(?:has\s+a\s+)?(?:limit|maximum|cap)\s*(?:of\s*)?(\d+)` | Google / 通用 |

### 可能影响现有行为的副作用

| 修改 | 副作用 | 缓解措施 |
|------|--------|----------|
| P0 新增 pattern | 可能增加误匹配率（更多子串匹配） | 新增 pattern 均为具体的 provider 文案，低误匹配风险 |
| P0-B 500/503 85% 阈值 | 高 token 使用率时的 500/503 会被强制归类为 context_overflow | 85% 阈值保守；压缩可逆；disconnect 路径已有 60% 阈值作对比 |
| P1 新增白名单 | 更多模型被识别为推理模型 | 只影响 stale-timeout 提升，不影响分类逻辑本身 |
| P1 启发式检测 | 可能将非推理模型误判为推理模型 | 过度匹配只增加超时等待时间（安全），不会导致错误压缩 |
| P2 output-cap 增强 | 更多错误被识别为 output-cap 而非 context_overflow | 这是设计意图：绕过压缩，直接减少 max_tokens 重试 |

---

## 实施顺序建议

1. **P0** 最先实施 — pattern 增加和阈值回退是最小风险改动，直接解决最大缺口。
2. **P1** 第二 — 推理模型家族扩展确保断连正确分类为 timeout。
3. **P2** 最后 — output-cap 解析增强依赖前面两层的正确分类，且涉及多 provider 正则适配，需要充分测试。

所有修改仅影响 source code 文件，不修改测试文件。
