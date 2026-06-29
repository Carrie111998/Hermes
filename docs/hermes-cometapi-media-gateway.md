# CometAPI 媒体数据面网关

状态：未来服务设计，不是 MVP 默认能力
来源：`docs/notion-source/hermes/pages/07-cometapi.md`

## 范围

CometAPI is described in the Notion source as a media data-plane gateway for images, audio, and video. It is separate from TokenRouter: TokenRouter handles control, credentials, policy, quota, and billing; CometAPI handles large binary/media retrieval, trimming, sampling, packaging, and native multimodal injection.

This service should not be built into the first web MVP unless the product needs long-video analysis, external social URL ingestion, or expensive multimodal preprocessing at scale.

## 职责

| 层级 | 职责 |
|---|---|
| Resolver | Fetch or resolve external media URLs such as YouTube, TikTok, Instagram, or direct uploads. |
| Physical preprocessing | Trim by time window, downsample frames, transcode resolution, extract audio and subtitles. |
| Multimodal packaging | Align frames, audio, transcript, and metadata by timestamp. |
| Model injection | Convert processed media into the target model's native multimodal parts. |
| Cache | Reuse processed chunks for repeated URL/time-window/fps/resolution requests. |

## 请求形态

CometAPI should be driven by tool calls such as `video_analyze` and `audio_analyze`.

```text
video_analyze(
  video_source,
  prompt,
  start_offset_sec?,
  end_offset_sec?,
  fps?,
  media_resolution?,
  text_only?
)
```

The gateway should accept a stable media ID or URL, not raw large binaries from the Agent prompt.

## 与 TokenRouter 的关系

| Concern | TokenRouter | CometAPI |
|---|---|---|
| JWT validation | Yes | Receives verified scoped request or validates delegated token. |
| Provider/API secrets | Yes | No direct exposure to sandbox. |
| Quota/billing | Yes | Emits usage signals back to TokenRouter/control plane. |
| Large binary fetch | No | Yes. |
| Frame/audio preprocessing | No | Yes. |
| Native multimodal payload | Routes request | Builds payload. |

## MVP 定位

For a first Hermes web MVP:

- Use normal upload storage and simple file parsing for small files.
- Keep `video_analyze` as a tool contract, but implement it with a simple worker path first.
- Add CometAPI only when repeated external video analysis, frame caching, or social-media scraping becomes a real bottleneck.

## 未来架构

```text
Agent tool call
  -> TokenRouter policy and quota check
  -> CometAPI delegated media request
  -> resolver/download/cache
  -> trim/downsample/transcode
  -> multimodal packaging
  -> model call through approved provider route
  -> result and usage event returned to Agent
```

## 失败策略

- External URL fetch failure should return a visible tool error, not silently switch to fake analysis.
- Text-only fallback is allowed only when the response explicitly marks that frames/audio were unavailable.
- Cached media chunks must be tenant-safe and should key on source, time window, fps, resolution, and access scope.
- Social media resolver failures must not leak proxy credentials or internal fetch details.

## 验收检查

- A 30-minute video can be analyzed through a bounded time window without placing all frames in prompt context.
- Repeated analysis of the same URL/time window can hit cache.
- Tenant A cannot reuse Tenant B's private upload cache.
- A failed resolver returns a structured error that the UI can display.
- TokenRouter can trace the CometAPI request into usage and billing records.
