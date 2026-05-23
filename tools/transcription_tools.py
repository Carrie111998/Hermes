1|#!/usr/bin/env python3
2|"""
3|Transcription Tools Module
4|
5|Provides speech-to-text transcription with six providers:
6|
7|  - **local** (default, free) — faster-whisper running locally, no API key needed.
8|    Auto-downloads the model (~150 MB for ``base``) on first use.
9|  - **groq** (free tier) — Groq Whisper API, requires ``GROQ_API_KEY``.
10|  - **openai** (paid) — OpenAI Whisper API, requires ``VOICE_TOOLS_OPENAI_KEY``.
11|  - **mistral** — Mistral Voxtral Transcribe API, requires ``MISTRAL_API_KEY``.
12|  - **xai** — xAI Grok STT API, requires ``XAI_API_KEY``. High accuracy,
13|    Inverse Text Normalization, diarization, 21 languages.
14|  - **elevenlabs** — ElevenLabs Scribe API, requires ``ELEVENLABS_API_KEY``.
15|
16|Used by the messaging gateway to automatically transcribe voice messages
17|sent by users on Telegram, Discord, WhatsApp, Slack, and Signal.
18|
19|Supported input formats: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, aac
20|
21|Usage::
22|
23|    from tools.transcription_tools import transcribe_audio
24|
25|    result = transcribe_audio("/path/to/audio.ogg")
26|    if result["success"]:
27|        print(result["transcript"])
28|"""
29|
30|import logging
31|import json
32|import os
33|import platform
34|import queue
35|import re
36|import shlex
37|import shutil
38|import subprocess
39|import tempfile
40|41|import threading
42|import time
43|

44|import time
45|import urllib.error
46|import urllib.request
47| (feat(stt): remote audio URL transcription with redirect handling and chunk fallback (#30657))
48|from pathlib import Path
49|from typing import Optional, Dict, Any
50|from urllib.parse import urljoin
51|
52|from hermes_cli._subprocess_compat import windows_hide_flags
53|from utils import is_truthy_value
54|from tools.managed_tool_gateway import resolve_managed_tool_gateway
55|from tools.tool_backend_helpers import (
56|    managed_nous_tools_enabled,
57|    nous_tool_gateway_unavailable_message,
58|    resolve_openai_audio_api_key,
59|)
60|
61|logger = logging.getLogger(__name__)
62|
63|def get_env_value(name, default=None):
64|    """Read env values through the live config module.
65|
66|    Tests may monkeypatch and later restore ``hermes_cli.config.get_env_value``
67|    before this module is imported. Resolve the helper at call time so STT does
68|    not keep a stale imported function for the rest of the test process.
69|    """
70|    try:
71|        from hermes_cli.config import get_env_value as _get_env_value
72|    except ImportError:
73|        return os.getenv(name, default)
74|    value = _get_env_value(name)
75|    return default if value is None else value
76|
77|
78|def _resolve_provider_key(env_var: str, provider_id: str) -> str:
79|    """Resolve an STT provider API key via the shared voice-key resolver.
80|
81|    Delegates to ``tools.tool_backend_helpers.resolve_provider_secret`` —
82|    the single owner of STT/TTS key resolution (config > env/.env > the
83|    credential pool populated by ``hermes auth add <provider_id>``).
84|    Resolved at call time so tests that reload the helpers module see the
85|    live function.
86|    """
87|    try:
88|        from tools.tool_backend_helpers import resolve_provider_secret
89|    except ImportError:  # pragma: no cover — helpers are in-repo
90|        return str(get_env_value(env_var) or "").strip()
91|    return resolve_provider_secret(env_var, provider_id, env_getter=get_env_value)
92|
93|# ---------------------------------------------------------------------------
94|# Optional imports — graceful degradation
95|# ---------------------------------------------------------------------------
96|
97|import importlib.util as _ilu
98|
99|
100|def _safe_find_spec(module_name: str) -> bool:
101|    try:
102|        return _ilu.find_spec(module_name) is not None
103|    except (ImportError, ValueError):
104|        return module_name in globals() or module_name in os.sys.modules
105|
106|
107|_HAS_FASTER_WHISPER = _safe_find_spec("faster_whisper")
108|_HAS_OPENAI = _safe_find_spec("openai")
109|_HAS_MISTRAL = _safe_find_spec("mistralai")
110|_HAS_PILK = _safe_find_spec("pilk")
111|
112|# ---------------------------------------------------------------------------
113|# Constants
114|# ---------------------------------------------------------------------------
115|
116|DEFAULT_PROVIDER = "local"
117|DEFAULT_LOCAL_MODEL = "base"
118|DEFAULT_LOCAL_STT_LANGUAGE = "en"
119|DEFAULT_STT_MODEL = os.getenv("STT_OPENAI_MODEL", "whisper-1")
120|DEFAULT_GROQ_STT_MODEL = os.getenv("STT_GROQ_MODEL", "whisper-large-v3-turbo")
121|DEFAULT_MISTRAL_STT_MODEL = os.getenv("STT_MISTRAL_MODEL", "voxtral-mini-latest")
122|DEFAULT_ELEVENLABS_STT_MODEL = os.getenv("STT_ELEVENLABS_MODEL", "scribe_v2")
123|LOCAL_STT_COMMAND_ENV = "HERMES_LOCAL_STT_COMMAND"
124|LOCAL_STT_LANGUAGE_ENV = "HERMES_LOCAL_STT_LANGUAGE"
125|COMMON_LOCAL_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
126|
127|GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
128|OPENAI_BASE_URL = os.getenv("STT_OPENAI_BASE_URL", "https://api.openai.com/v1")
129|XAI_STT_BASE_URL = os.getenv("XAI_STT_BASE_URL", "https://api.x.ai/v1")
130|ELEVENLABS_STT_BASE_URL = os.getenv("ELEVENLABS_STT_BASE_URL", "https://api.elevenlabs.io/v1")
131|# DeepInfra STT base URL now resolved via hermes_cli.models.deepinfra_base_url (shared).
132|
133|SUPPORTED_FORMATS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".oga", ".opus", ".aac", ".flac", ".caf"}
134|LOCAL_NATIVE_AUDIO_FORMATS = {".wav", ".aiff", ".aif"}
135|MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB
136|CHUNK_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB chunk target (under Groq's 25 MB free limit)
137|
138|# Known model sets for auto-correction
139|OPENAI_MODELS = {"whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-transcribe"}
140|GROQ_MODELS = {"whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"}
141|
142|# Singleton for the local model — loaded once, reused across calls
143|_local_model: Optional[object] = None
144|_local_model_name: Optional[str] = None
145|# Guards the check-then-load of the module-global model cache above.
146|# Without it, two concurrent voice messages can both see `_local_model is
147|# None` and download/load the whisper model twice (#24767).
148|_local_model_lock = threading.Lock()
149|
150|# --- Idle unload ---------------------------------------------------------------
151|# The model singleton above is loaded once and never released — hundreds of MB
152|# of RAM/VRAM sit idle between voice messages. On long-running gateway
153|# processes (especially with local LLMs competing for the same GPU) this is
154|# wasteful. A single long-lived daemon thread checks _last_transcription_time
155|# and unloads the model after a configurable idle period, then exits. The next
156|# voice message reloads the model and restarts the watcher transparently.
157|_last_transcription_time: float = 0.0
158|_idle_unload_thread: Optional[threading.Thread] = None
159|_idle_unload_stop = threading.Event()
160|# Serializes watcher start checks so two concurrent transcriptions can't
161|# both observe "no watcher alive" and spawn duplicates.
162|_idle_unload_mgmt_lock = threading.Lock()
163|
164|_IDLE_UNLOAD_CHECK_INTERVAL = 30  # seconds between idle checks
165|
166|# ---------------------------------------------------------------------------
167|# Config helpers
168|# ---------------------------------------------------------------------------
169|
170|
171|
172|def _load_stt_config() -> dict:
173|    """Load the ``stt`` section from user config, falling back to defaults."""
174|    try:
175|        from hermes_cli.config import load_config
176|        return load_config().get("stt") or {}
177|    except Exception:
178|        return {}
179|
180|
181|def is_stt_enabled(stt_config: Optional[dict] = None) -> bool:
182|    """Return whether STT is enabled in config."""
183|    if stt_config is None:
184|        stt_config = _load_stt_config()
185|    enabled = stt_config.get("enabled", True)
186|    return is_truthy_value(enabled, default=True)
187|
188|
189|def _resolve_stt_language(
190|    provider_key: str,
191|    stt_config: Optional[Dict[str, Any]] = None,
192|    *,
193|    extra_keys: tuple = (),
194|) -> Optional[str]:
195|    """Resolve the language hint for an STT provider (class-level, all providers).
196|
197|    Resolution order (first non-empty wins):
198|      1. ``stt.<provider>.language`` (plus any *extra_keys* aliases, e.g.
199|         ElevenLabs' historical ``language_code``)
200|      2. ``stt.language``           — global default for every provider
201|      3. ``HERMES_LOCAL_STT_LANGUAGE`` env var (legacy escape hatch)
202|      4. ``None``                   — let the provider auto-detect
203|
204|    Returns a stripped ISO-639-1-ish code or None. Never returns "".
205|    """
206|    if stt_config is None:
207|        stt_config = _load_stt_config()
208|    provider_cfg = _get_stt_section(stt_config, provider_key)
209|    candidates = [provider_cfg.get("language")]
210|    for key in extra_keys:
211|        candidates.append(provider_cfg.get(key))
212|    if isinstance(stt_config, dict):
213|        candidates.append(stt_config.get("language"))
214|    candidates.append(os.getenv(LOCAL_STT_LANGUAGE_ENV))
215|    for candidate in candidates:
216|        if isinstance(candidate, str) and candidate.strip():
217|            return candidate.strip()
218|    return None
219|
220|
221|def _has_openai_audio_backend() -> bool:
222|    """Return True when OpenAI audio can use config credentials, env credentials, or the managed gateway."""
223|    try:
224|        _resolve_openai_audio_client_config()
225|        return True
226|    except ValueError:
227|        return False
228|
229|
230|def _find_binary(binary_name: str) -> Optional[str]:
231|    """Find a local binary, checking common Homebrew/local prefixes as well as PATH."""
232|    for directory in COMMON_LOCAL_BIN_DIRS:
233|        candidate = Path(directory) / binary_name
234|        if candidate.exists() and os.access(candidate, os.X_OK):
235|            return str(candidate)
236|    return shutil.which(binary_name)
237|
238|
239|def _find_ffmpeg_binary() -> Optional[str]:
240|    return _find_binary("ffmpeg")
241|
242|
243|# Shared encode profile for every STT-bound m4a we produce (transcode and
244|# silence-trim): 16 kHz mono 32 kbps AAC, faststart. One owner — codec or
245|# bitrate changes must not drift between the two paths.
246|_STT_M4A_ENCODE_ARGS = (
247|    "-vn", "-ac", "1", "-ar", "16000",
248|    "-c:a", "aac", "-b:a", "32k", "-movflags", "+faststart",
249|)
250|
251|
252|def _run_ffmpeg_stt_encode(
253|    ffmpeg: str, input_path: str, output_path: str, *, audio_filter: Optional[str] = None
254|) -> None:
255|    """Run the shared STT m4a encode, optionally with an ``-af`` filter.
256|
257|    Raises on failure (CalledProcessError / TimeoutExpired) — callers own
258|    the error semantics (transcode reports, trim swallows).
259|    """
260|    command = [ffmpeg, "-y", "-i", input_path]
261|    if audio_filter:
262|        command += ["-af", audio_filter]
263|    command += [*_STT_M4A_ENCODE_ARGS, output_path]
264|    subprocess.run(
265|        command, check=True, capture_output=True, text=True,
266|        encoding="utf-8", errors="replace", timeout=120,
267|        stdin=subprocess.DEVNULL, creationflags=windows_hide_flags(),
268|    )
269|
270|
271|def _transcode_audio_for_stt(file_path: str, work_dir: str) -> tuple[Optional[str], Optional[str]]:
272|    """Transcode ``file_path`` to a compact, broadly-accepted .m4a for STT upload.
273|
274|    Newer OpenAI transcription models (``gpt-4o-transcribe``,
275|    ``gpt-4o-mini-transcribe``) reject some containers the legacy ``whisper-1``
276|    endpoint accepted -- notably the Ogg/Opus voice notes messaging apps send --
277|    and gateway downloads occasionally arrive with a misleading extension.
278|    Normalizing to 16 kHz mono AAC/m4a produces a small file the endpoints
279|    accept. Returns ``(converted_path, None)`` on success or ``(None, error)``.
280|    """
281|    ffmpeg = _find_ffmpeg_binary()
282|    if not ffmpeg:
283|        return None, "audio needs transcoding for the STT API, but ffmpeg was not found"
284|    converted_path = os.path.join(work_dir, f"{Path(file_path).stem or 'audio'}-stt.m4a")
285|    try:
286|        _run_ffmpeg_stt_encode(ffmpeg, file_path, converted_path)
287|        return converted_path, None
288|    except subprocess.CalledProcessError as exc:
289|        details = exc.stderr.strip() or exc.stdout.strip() or str(exc)
290|        logger.error("ffmpeg STT transcode failed for %s: %s", file_path, details)
291|        return None, f"failed to transcode audio for the STT API: {details}"
292|    except Exception as exc:  # noqa: BLE001 - transcode is best-effort
293|        logger.error("unexpected STT transcode failure for %s: %s", file_path, exc, exc_info=True)
294|        return None, f"failed to transcode audio for the STT API: {exc}"
295|
296|
297|def _find_whisper_binary() -> Optional[str]:
298|    return _find_binary("whisper")
299|
300|
301|def _get_local_command_template() -> Optional[str]:
302|    configured = os.getenv(LOCAL_STT_COMMAND_ENV, "").strip()
303|    if configured:
304|        return configured
305|
306|    whisper_binary = _find_whisper_binary()
307|    if whisper_binary:
308|        quoted_binary = shlex.quote(whisper_binary)
309|        return (
310|            f"{quoted_binary} {{input_path}} --model {{model}} --output_format txt "
311|            "--output_dir {output_dir} --language {language}"
312|        )
313|    return None
314|
315|
316|def _has_local_command() -> bool:
317|    return _get_local_command_template() is not None
318|
319|
320|# ---------------------------------------------------------------------------
321|# Remote URL helpers
322|# ---------------------------------------------------------------------------
323|
324|
325|def _probe_audio_url(url: str) -> Dict[str, Any]:
326|    """Probe a remote audio URL with a HEAD request, following redirects.
327|
328|    Returns:
329|        dict with keys:
330|          - success (bool)
331|          - url (str): final URL after redirects
332|          - content_type (str or None)
333|          - content_length (int or None)
334|          - error (str, optional)
335|    """
336|    try:
337|        req = urllib.request.Request(url, method="HEAD")
338|        req.add_header("User-Agent", "Hermes-Agent/1.0")
339|        with urllib.request.urlopen(req, timeout=30) as resp:
340|            final_url = resp.url
341|            content_type = resp.headers.get("Content-Type")
342|            content_length_str = resp.headers.get("Content-Length")
343|            content_length = int(content_length_str) if content_length_str else None
344|            return {
345|                "success": True,
346|                "url": final_url,
347|                "content_type": content_type,
348|                "content_length": content_length,
349|            }
350|    except urllib.error.HTTPError as e:
351|        return {
352|            "success": False,
353|            "url": url,
354|            "content_type": None,
355|            "content_length": None,
356|            "error": f"HTTP {e.code}: {e.reason}",
357|        }
358|    except urllib.error.URLError as e:
359|        return {
360|            "success": False,
361|            "url": url,
362|            "content_type": None,
363|            "content_length": None,
364|            "error": f"URL error: {e.reason}",
365|        }
366|    except Exception as e:
367|        return {
368|            "success": False,
369|            "url": url,
370|            "content_type": None,
371|            "content_length": None,
372|            "error": str(e),
373|        }
374|
375|
376|def _download_audio(url: str) -> Dict[str, Any]:
377|    """Download audio from a URL to a temporary file.
378|
379|    Returns:
380|        dict with keys:
381|          - success (bool)
382|          - file_path (str, optional): path to the downloaded temp file
383|          - error (str, optional)
384|    """
385|    try:
386|        req = urllib.request.Request(url)
387|        req.add_header("User-Agent", "Hermes-Agent/1.0")
388|        with urllib.request.urlopen(req, timeout=60) as resp:
389|            data = resp.read()
390|            # Guess extension from Content-Type
391|            content_type = resp.headers.get("Content-Type", "")
392|            ext = ".mp3"  # default
393|            if "ogg" in content_type or "opus" in content_type:
394|                ext = ".ogg"
395|            elif "wav" in content_type or "wave" in content_type:
396|                ext = ".wav"
397|            elif "m4a" in content_type or "mp4" in content_type:
398|                ext = ".m4a"
399|            elif "webm" in content_type:
400|                ext = ".webm"
401|            elif "flac" in content_type:
402|                ext = ".flac"
403|            elif "aac" in content_type:
404|                ext = ".aac"
405|
406|            fd, file_path = tempfile.mkstemp(suffix=ext, prefix="hermes-url-audio-")
407|            with os.fdopen(fd, "wb") as f:
408|                f.write(data)
409|
410|        return {"success": True, "file_path": file_path}
411|    except urllib.error.HTTPError as e:
412|        return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
413|    except urllib.error.URLError as e:
414|        return {"success": False, "error": f"URL error: {e.reason}"}
415|    except Exception as e:
416|        return {"success": False, "error": str(e)}
417|
418|
419|def _split_audio(
420|    input_path: str,
421|    output_dir: str,
422|    max_bytes: int = CHUNK_SIZE_LIMIT,
423|) -> Dict[str, Any]:
424|    """Split a large audio file into chunks below ``max_bytes`` using ffmpeg.
425|
426|    Args:
427|        input_path: Path to the audio file.
428|        output_dir: Directory to write chunks into.
429|        max_bytes: Approximate max bytes per chunk (default: CHUNK_SIZE_LIMIT).
430|
431|    Returns:
432|        dict with keys:
433|          - success (bool)
434|          - chunks (list[str], optional): list of chunk file paths
435|          - error (str, optional)
436|    """
437|    ffmpeg = _find_ffmpeg_binary()
438|    if not ffmpeg:
439|        return {"success": False, "error": "ffmpeg not found"}
440|
441|    try:
442|        # Get duration with ffprobe
443|        probe_cmd = [
444|            _find_binary("ffprobe") or "ffprobe",
445|            "-v",
446|            "quiet",
447|            "-print_format",
448|            "json",
449|            "-show_format",
450|            input_path,
451|        ]
452|        probe_result = subprocess.run(
453|            probe_cmd, check=True, capture_output=True, text=True, timeout=30
454|        )
455|        probe_data = json.loads(probe_result.stdout)
456|        duration = float(probe_data.get("format", {}).get("duration", 0))
457|        total_bytes = int(probe_data.get("format", {}).get("size", 0))
458|
459|        if duration <= 0 or total_bytes <= 0:
460|            return {
461|                "success": False,
462|                "error": f"Could not determine audio duration/size: duration={duration}, bytes={total_bytes}",
463|            }
464|
465|        # Calculate chunk duration
466|        bytes_per_sec = total_bytes / duration
467|        chunk_duration = max(int(max_bytes / bytes_per_sec), 5)  # min 5s per chunk
468|
469|        # Split with ffmpeg
470|        base = os.path.splitext(os.path.basename(input_path))[0]
471|        ext = os.path.splitext(input_path)[1] or ".mp3"
472|        output_pattern = os.path.join(output_dir, f"{base}_chunk_%03d{ext}")
473|
474|        split_cmd = [
475|            ffmpeg,
476|            "-y",
477|            "-i",
478|            input_path,
479|            "-f",
480|            "segment",
481|            "-segment_time",
482|            str(chunk_duration),
483|            "-c",
484|            "copy",
485|            "-map",
486|            "0",
487|            output_pattern,
488|        ]
489|
490|        subprocess.run(split_cmd, check=True, capture_output=True, text=True, timeout=300)
491|
492|        # Collect chunk files in order
493|        chunks = sorted(
494|            os.path.join(output_dir, f)
495|            for f in os.listdir(output_dir)
496|            if f.startswith(f"{base}_chunk_")
497|        )
498|
499|        if not chunks:
500|            return {"success": False, "error": "ffmpeg split produced no chunk files"}
501|
502|        return {"success": True, "chunks": chunks}
503|
504|    except subprocess.TimeoutExpired:
505|        return {"success": False, "error": "ffmpeg split timed out"}
506|    except subprocess.CalledProcessError as e:
507|        detail = e.stderr.strip() or e.stdout.strip() or str(e)
508|        logger.error("ffmpeg split failed for %s: %s", input_path, detail)
509|        return {"success": False, "error": f"ffmpeg split failed: {detail}"}
510|    except Exception as e:
511|        logger.error("Split audio failed: %s", e, exc_info=True)
512|        return {"success": False, "error": str(e)}
513|
514|
515|def _transcribe_chunks(
516|    chunk_paths: list,
517|    model_name: str,
518|) -> Dict[str, Any]:
519|    """Transcribe audio chunks sequentially via Groq and merge with segment markers.
520|
521|    Args:
522|        chunk_paths: Ordered list of chunk file paths.
523|        model_name: STT model name.
524|
525|    Returns:
526|        dict with keys:
527|          - success (bool)
528|          - transcript (str): merged transcript with [MM:SS] markers
529|          - error (str, optional)
530|    """
531|    merged_parts = []
532|    chunk_duration = None  # will be inferred from ffprobe
533|
534|    for i, chunk_path in enumerate(chunk_paths):
535|        logger.info("Transcribing chunk %d/%d: %s", i + 1, len(chunk_paths), chunk_path)
536|
537|        # Get chunk duration for timestamp
538|        if chunk_duration is None:
539|            try:
540|                probe_cmd = [
541|                    _find_binary("ffprobe") or "ffprobe",
542|                    "-v",
543|                    "quiet",
544|                    "-print_format",
545|                    "json",
546|                    "-show_format",
547|                    chunk_path,
548|                ]
549|                probe_result = subprocess.run(
550|                    probe_cmd, check=True, capture_output=True, text=True, timeout=15
551|                )
552|                import json as _json
553|
554|                probe_data = _json.loads(probe_result.stdout)
555|                chunk_duration = float(probe_data.get("format", {}).get("duration", 0))
556|            except Exception:
557|                chunk_duration = 0
558|
559|        start_seconds = i * (chunk_duration or 0)
560|        minutes = int(start_seconds // 60)
561|        seconds = int(start_seconds % 60)
562|        marker = f"[{minutes:02d}:{seconds:02d}]"
563|
564|        # Transcribe this chunk
565|        result = _transcribe_groq(chunk_path, model_name)
566|        if not result["success"]:
567|            merged_parts.append(f"{marker} [TRANSCRIPTION ERROR: {result.get('error', 'unknown')}]")
568|        else:
569|            transcript = result["transcript"].strip()
570|            if transcript:
571|                merged_parts.append(f"{marker} {transcript}")
572|
573|        # Rate-limit between chunks
574|        if i < len(chunk_paths) - 1:
575|            time.sleep(1)
576|
577|        # Clean up the temp chunk file
578|        try:
579|            os.remove(chunk_path)
580|        except OSError:
581|            pass
582|
583|    merged = "\n\n".join(merged_parts) if merged_parts else ""
584|    return {"success": True, "transcript": merged, "provider": "groq"}
585|
586|
587|def _normalize_local_model(model_name: Optional[str]) -> str:
588|    """Return a valid faster-whisper model size, mapping cloud-only names to the default.
589|
590|    Cloud providers like OpenAI use names such as ``whisper-1`` which are not
591|    valid for faster-whisper (which expects ``tiny``, ``base``, ``small``,
592|    ``medium``, or ``large-v*``).  When such a name is detected we fall back to
593|    the default local model and emit a warning so the user knows what happened.
594|    """
595|    if not model_name or model_name in OPENAI_MODELS or model_name in GROQ_MODELS:
596|        if model_name and (model_name in OPENAI_MODELS or model_name in GROQ_MODELS):
597|            logger.warning(
598|                "STT model '%s' is a cloud-only name and cannot be used with the local "
599|                "provider. Falling back to '%s'. Set stt.local.model to a valid "
600|                "faster-whisper size (tiny, base, small, medium, large-v3).",
601|                model_name,
602|                DEFAULT_LOCAL_MODEL,
603|            )
604|        return DEFAULT_LOCAL_MODEL
605|    return model_name
606|
607|
608|def _normalize_local_command_model(model_name: Optional[str]) -> str:
609|    return _normalize_local_model(model_name)
610|
611|
612|def _try_lazy_install_stt() -> bool:
613|    """Attempt to lazy-install faster-whisper and return True on success.
614|
615|    The module-level ``_HAS_FASTER_WHISPER`` flag is set at import time and
616|    cached. If the package wasn't installed at startup, calling ``ensure()``
617|    installs it. This function re-checks dynamically after installation so
618|    the provider can use it immediately without a process restart.
619|    """
620|    try:
621|        from tools.lazy_deps import ensure
622|        # prompt=False: never raise a blocking input() prompt mid-session.
623|        # Under the interactive CLI prompt_toolkit owns stdin, so a bare
624|        # input() deadlocks the terminal (#40490). The install is already
625|        # gated by security.allow_lazy_installs, so reaching here is opt-in.
626|        ensure("stt.faster_whisper", prompt=False)
627|        # Re-check dynamically after install
628|        import importlib.util as _iu
629|        if _iu.find_spec("faster_whisper"):
630|            return True
631|        logger.warning(
632|            "faster-whisper was installed but importlib still cannot find it "
633|            "(may require Python restart)"
634|        )
635|    except Exception as exc:
636|        logger.warning(
637|            "Lazy install of faster-whisper failed: %s. "
638|            "This is often a permission issue: the Hermes process user cannot "
639|            "write to the virtual environment. Try running manually as the "
640|            "venv owner: `stat -c '%%u' '$(dirname $(dirname $(which python3)))'` "
641|            "then `su - <owner> -c 'VIRTUAL_ENV=/opt/hermes/.venv "
642|            "uv pip install faster-whisper==1.2.1'`",
643|            exc,
644|        )
645|    return False
646|
647|
648|# Names of the STT providers with native handlers in this module.
649|# Kept in sync with ``agent.transcription_registry._BUILTIN_NAMES`` —
650|# a regression test fails if they drift. The plugin hook from
651|# issue #30398-style follow-up rejects plugins registering under any
652|# of these names; the dispatcher in ``transcribe_audio`` short-circuits
653|# them defensively as well.
654|BUILTIN_STT_PROVIDERS = frozenset({
655|    "local",
656|    "local_command",
657|    "groq",
658|    "openai",
659|    "mistral",
660|    "xai",
661|    "elevenlabs",
662|    "deepinfra",
663|})
664|
665|
666|# ---------------------------------------------------------------------------
667|# Command-provider registry (``stt.providers.<name>: type: command``)
668|# ---------------------------------------------------------------------------
669|#
670|# Mirrors the TTS command-provider registry shipped in PR #17843 — same
671|# placeholder grammar, same shell-quote-aware rendering, same process-tree
672|# termination on timeout. Lets any whisper CLI / ASR CLI / curl pipeline
673|# become an STT backend with zero Python.
674|#
675|# Resolution order:
676|#   1. Built-in (``local``, ``local_command``, ``groq``, ``openai``,
677|#      ``mistral``, ``xai``)              → native handler. **Always wins.**
678|#   2. ``stt.providers.<name>: type: command``  → command-provider runner.
679|#   3. Plugin-registered TranscriptionProvider  → plugin dispatch.
680|#   4. No match                                 → "No STT provider available".
681|#
682|# The single-env-var ``HERMES_LOCAL_STT_COMMAND`` escape hatch is preserved
683|# untouched via the built-in ``local_command`` path. Use the command-provider
684|# registry when you want MULTIPLE shell-driven STT engines, or you want a
685|# named provider you can pick via ``stt.provider`` in config.yaml.
686|DEFAULT_COMMAND_STT_TIMEOUT_SECONDS = 300
687|DEFAULT_COMMAND_STT_LANGUAGE = "en"
688|DEFAULT_COMMAND_STT_OUTPUT_FORMAT = "txt"
689|COMMAND_STT_OUTPUT_FORMATS = frozenset({"txt", "json", "srt", "vtt"})
690|
691|
692|def _get_stt_section(stt_config: Dict[str, Any], name: str) -> Dict[str, Any]:
693|    """Return an stt sub-section if it's a dict, else an empty dict."""
694|    if not isinstance(stt_config, dict):
695|        return {}
696|    section = stt_config.get(name)
697|    return section if isinstance(section, dict) else {}
698|
699|
700|def _get_named_stt_provider_config(
701|    stt_config: Dict[str, Any],
702|    name: str,
703|) -> Dict[str, Any]:
704|    """Return the config dict for a user-declared STT command provider.
705|
706|    Looks up ``stt.providers.<name>`` first (the canonical location), and
707|    falls back to ``stt.<name>`` so users who followed the built-in layout
708|    still work. Returns an empty dict when the provider is not declared.
709|
710|    Built-in names are NOT special-cased here — the caller short-circuits
711|    them before this is consulted, AND ``_is_command_stt_provider_config``
712|    requires an explicit ``command:`` value, so a built-in section like
713|    ``stt.openai`` (which has ``model``/``language`` but no ``command``)
714|    can't accidentally be treated as a command provider.
715|    """
716|    providers = _get_stt_section(stt_config, "providers")
717|    section = providers.get(name) if isinstance(providers, dict) else None
718|    if isinstance(section, dict):
719|        return section
720|    # Back-compat: allow ``stt.<name>`` for user-declared providers too,
721|    # but only when the name is not a built-in (so a user's ``stt.openai``
722|    # block still means the OpenAI provider, not a custom command).
723|    if name.lower() not in BUILTIN_STT_PROVIDERS:
724|        legacy = _get_stt_section(stt_config, name)
725|        if legacy:
726|            return legacy
727|    return {}
728|
729|
730|def _is_command_stt_provider_config(config: Dict[str, Any]) -> bool:
731|    """Return True when *config* declares a command-type STT provider."""
732|    if not isinstance(config, dict):
733|        return False
734|    ptype = str(config.get("type") or "").strip().lower()
735|    if ptype and ptype != "command":
736|        return False
737|    command = config.get("command")
738|    return isinstance(command, str) and bool(command.strip())
739|
740|
741|def _resolve_command_stt_provider_config(
742|    provider: str,
743|    stt_config: Dict[str, Any],
744|) -> Optional[Dict[str, Any]]:
745|    """Return the provider config if *provider* resolves to a command type.
746|
747|    Built-in provider names are rejected (they have native handlers).
748|    Returns None when the name is a built-in, ``"none"``, unknown, or not
749|    a command type.
750|    """
751|    if not provider:
752|        return None
753|    key = provider.lower().strip()
754|    if key in BUILTIN_STT_PROVIDERS or key == "none":
755|        return None
756|    config = _get_named_stt_provider_config(stt_config, key)
757|    if _is_command_stt_provider_config(config):
758|        return config
759|    return None
760|
761|
762|def _is_local_stt_provider(provider: str, stt_config: Dict[str, Any]) -> bool:
763|    """Return whether *provider* is exempt from Hermes's remote upload cap."""
764|    key = (provider or "").lower().strip()
765|    if key in {"local", "local_command"}:
766|        return True
767|    return False
768|
769|
770|def _iter_command_stt_providers(stt_config: Dict[str, Any]):
771|    """Yield (name, config) pairs for every declared command-type STT provider."""
772|    if not isinstance(stt_config, dict):
773|        return
774|    providers = _get_stt_section(stt_config, "providers")
775|    for name, cfg in (providers or {}).items():
776|        if isinstance(name, str) and name.lower() not in BUILTIN_STT_PROVIDERS:
777|            if _is_command_stt_provider_config(cfg):
778|                yield name, cfg
779|
780|
781|def _has_any_command_stt_provider(stt_config: Optional[Dict[str, Any]] = None) -> bool:
782|    """Return True when any command-type STT provider is configured."""
783|    if stt_config is None:
784|        stt_config = _load_stt_config()
785|    for _name, _cfg in _iter_command_stt_providers(stt_config):
786|        return True
787|    return False
788|
789|
790|def _get_command_stt_timeout(config: Dict[str, Any]) -> float:
791|    """Return timeout in seconds, falling back when invalid."""
792|    raw = config.get("timeout", config.get("timeout_seconds", DEFAULT_COMMAND_STT_TIMEOUT_SECONDS))
793|    try:
794|        value = float(raw)
795|    except (TypeError, ValueError):
796|        return float(DEFAULT_COMMAND_STT_TIMEOUT_SECONDS)
797|    if value <= 0:
798|        return float(DEFAULT_COMMAND_STT_TIMEOUT_SECONDS)
799|    return value
800|
801|
802|def _get_command_stt_output_format(config: Dict[str, Any]) -> str:
803|    """Return the validated output format (txt/json/srt/vtt)."""
804|    raw = (
805|        config.get("format")
806|        or config.get("output_format")
807|        or DEFAULT_COMMAND_STT_OUTPUT_FORMAT
808|    )
809|    fmt = str(raw).lower().strip().lstrip(".")
810|    return fmt if fmt in COMMAND_STT_OUTPUT_FORMATS else DEFAULT_COMMAND_STT_OUTPUT_FORMAT
811|
812|
813|def _shell_quote_context_stt(command_template: str, position: int) -> Optional[str]:
814|    """Return the shell quote character active right before *position*.
815|
816|    Mirrors ``tools.tts_tool._shell_quote_context`` — kept local to avoid
817|    cross-module import of a private helper. Returns ``"'"`` / ``'"'`` when
818|    inside a quoted region, ``None`` for bare context.
819|    """
820|    quote: Optional[str] = None
821|    escaped = False
822|    i = 0
823|    while i < position:
824|        char = command_template[i]
825|        if quote == "'":
826|            if char == "'":
827|                quote = None
828|        elif quote == '"':
829|            if escaped:
830|                escaped = False
831|            elif char == "\\":
832|                escaped = True
833|            elif char == '"':
834|                quote = None
835|        elif char == "'":
836|            quote = "'"
837|        elif char == '"':
838|            quote = '"'
839|        elif char == "\\":
840|            i += 1
841|        i += 1
842|    return quote
843|
844|
845|def _quote_command_stt_placeholder(value: str, quote_context: Optional[str]) -> str:
846|    """Quote a placeholder value for its position in a shell command template.
847|
848|    Mirrors ``tools.tts_tool._quote_command_tts_placeholder``.
849|    """
850|    if quote_context == "'":
851|        return value.replace("'", r"'\''")
852|    if quote_context == '"':
853|        return (
854|            value
855|            .replace("\\", "\\\\")
856|            .replace('"', r'\"')
857|            .replace("$", r"\$")
858|            .replace("`", r"\`")
859|        )
860|    if os.name == "nt":
861|        return subprocess.list2cmdline([value])
862|    return shlex.quote(value)
863|
864|
865|def _render_command_stt_template(
866|    command_template: str,
867|    placeholders: Dict[str, str],
868|) -> str:
869|    """Replace supported placeholders while preserving ``{{`` / ``}}``.
870|
871|    Mirrors ``tools.tts_tool._render_command_tts_template``. Placeholders
872|    are shell-quote-aware: ``{voice}`` inside single quotes gets
873|    single-quote-safe escaping, inside double quotes gets ``$``/`` ` ``/`` " ``
874|    escaping, outside quotes gets ``shlex.quote``. Doubled braces ``{{`` and
875|    ``}}`` are preserved as literal ``{`` / ``}`` for users who want to
876|    embed JSON snippets in their command.
877|    """
878|    import re
879|
880|    names = "|".join(re.escape(name) for name in placeholders)
881|    pattern = re.compile(
882|        rf"(?<!\$)(?:\{{\{{(?P<double>{names})\}}\}}|\{{(?P<single>{names})\}})"
883|    )
884|    replacements: list[tuple[str, str]] = []
885|
886|    def replace_match(match: "re.Match[str]") -> str:
887|        name = match.group("double") or match.group("single")
888|        token = f"__HERMES_STT_PLACEHOLDER_{len(replacements)}__"
889|        replacements.append((
890|            token,
891|            _quote_command_stt_placeholder(
892|                placeholders[name],
893|                _shell_quote_context_stt(command_template, match.start()),
894|            ),
895|        ))
896|        return token
897|
898|    rendered = pattern.sub(replace_match, command_template)
899|    rendered = rendered.replace("{{", "{").replace("}}", "}")
900|    for token, value in replacements:
901|        rendered = rendered.replace(token, value)
902|    return rendered
903|
904|
905|def _terminate_command_stt_process_tree(proc: subprocess.Popen) -> None:
906|    """Best-effort termination of a shell process and all of its children.
907|
908|    Mirrors ``tools.tts_tool._terminate_command_tts_process_tree``.
909|    """
910|    if proc.poll() is not None:
911|        return
912|
913|    if os.name == "nt":
914|        try:
915|            subprocess.run(
916|                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
917|                stdout=subprocess.DEVNULL,
918|                stderr=subprocess.DEVNULL,
919|                timeout=5,
920|                stdin=subprocess.DEVNULL,
921|            )
922|        except Exception:
923|            proc.kill()
924|        return
925|
926|    try:
927|        import psutil  # type: ignore
928|    except ImportError:
929|        # psutil is optional — fall back to single-process terminate/kill
930|        proc.terminate()
931|        try:
932|            proc.wait(timeout=2)
933|        except subprocess.TimeoutExpired:
934|            proc.kill()
935|        return
936|
937|    try:
938|        parent = psutil.Process(proc.pid)
939|        for child in parent.children(recursive=True):
940|            try:
941|                child.terminate()
942|            except psutil.NoSuchProcess:
943|                pass
944|        parent.terminate()
945|    except psutil.NoSuchProcess:
946|        return
947|    except Exception:
948|        proc.terminate()
949|
950|    try:
951|        proc.wait(timeout=2)
952|        return
953|    except subprocess.TimeoutExpired:
954|        pass
955|
956|    try:
957|        parent = psutil.Process(proc.pid)
958|        for child in parent.children(recursive=True):
959|            try:
960|                child.kill()
961|            except psutil.NoSuchProcess:
962|                pass
963|        parent.kill()
964|    except psutil.NoSuchProcess:
965|        return
966|    except Exception:
967|        proc.kill()
968|
969|
970|def _command_stt_env_passthrough(config: Dict[str, Any]) -> list:
971|    """Return the provider's ``env_passthrough`` allowlist (opt-out of scrub).
972|
973|    Command providers legitimately reference their own API keys in the shell
974|    template (curl one-liners). The child env is scrubbed of Hermes secrets by
975|    default; ``env_passthrough: [MY_API_KEY, ...]`` copies the named variables
976|    back from the parent environment so a trusted template keeps working.
977|    Mirrors ``tools.tts_tool._command_provider_env_passthrough``.
978|    """
979|    raw = config.get("env_passthrough")
980|    if not isinstance(raw, (list, tuple)):
981|        return []
982|    return [str(item).strip() for item in raw if str(item).strip()]
983|
984|
985|def _run_command_stt(
986|    command: str,
987|    timeout: float,
988|    env_passthrough: Optional[list] = None,
989|) -> subprocess.CompletedProcess:
990|    """Run a command-provider shell command with process-tree idle cleanup.
991|
992|    Mirrors ``tools.tts_tool._run_command_tts``: ``timeout`` is an IDLE
993|    timeout, reset whenever the command emits output on stdout/stderr —
994|    a slow-but-alive provider survives, a silently stalled one is killed
995|    (same progress-based stuck detection as the TTS runner, #50081).
996|    Child env is scrubbed of Hermes secrets (salvage of #56332) while still
997|    propagating delegated-child lineage markers when applicable.
998|    """
999|    from agent.delegation_context import delegated_child_subprocess_env
1000|    from tools.environments.local import hermes_subprocess_env
1001|
1002|    scrubbed = hermes_subprocess_env(inherit_credentials=False)
1003|    for key in env_passthrough or []:
1004|        value = os.environ.get(key)
1005|        if value is not None:
1006|            scrubbed[key] = value
1007|    popen_kwargs: Dict[str, Any] = {
1008|        "shell": True,
1009|        "stdout": subprocess.PIPE,
1010|        "stderr": subprocess.PIPE,
1011|        "text": True,
1012|        # Lossy UTF-8 decode — locale-mismatched bytes from the STT command
1013|        # must not raise in the reader threads on non-UTF-8 Windows (#45099).
1014|        "encoding": "utf-8",
1015|        "errors": "replace",
1016|        "env": delegated_child_subprocess_env(scrubbed),
1017|    }
1018|    if os.name == "nt":
1019|        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
1020|    else:
1021|        popen_kwargs["start_new_session"] = True
1022|
1023|    proc = subprocess.Popen(command, **popen_kwargs, stdin=subprocess.DEVNULL)
1024|    output_queue: "queue.Queue[tuple[str, Optional[str]]]" = queue.Queue()
1025|    chunks: Dict[str, list] = {"stdout": [], "stderr": []}
1026|    open_streams = {"stdout", "stderr"}
1027|
1028|    def read_stream(name: str, stream: Any) -> None:
1029|        encoding = getattr(stream, "encoding", None) or "utf-8"
1030|        read1 = getattr(getattr(stream, "buffer", None), "read1", None)
1031|        try:
1032|            while True:
1033|                if read1 is None:
1034|                    chunk = stream.read(65536)
1035|                else:
1036|                    data = read1(65536)
1037|                    chunk = data.decode(encoding, errors="replace")
1038|                if not chunk:
1039|                    break
1040|                output_queue.put((name, chunk))
1041|        finally:
1042|            output_queue.put((name, None))
1043|
1044|    readers = [
1045|        threading.Thread(
1046|            target=read_stream,
1047|            args=("stdout", proc.stdout),
1048|            daemon=True,
1049|        ),
1050|        threading.Thread(
1051|            target=read_stream,
1052|            args=("stderr", proc.stderr),
1053|            daemon=True,
1054|        ),
1055|    ]
1056|    for reader in readers:
1057|        reader.start()
1058|
1059|    deadline = time.monotonic() + timeout
1060|    timed_out = False
1061|    while open_streams:
1062|        remaining = deadline - time.monotonic()
1063|        if remaining <= 0:
1064|            timed_out = True
1065|            break
1066|        try:
1067|            name, chunk = output_queue.get(timeout=min(0.05, remaining))
1068|        except queue.Empty:
1069|            continue
1070|        if chunk is None:
1071|            open_streams.discard(name)
1072|            continue
1073|        chunks[name].append(chunk)
1074|        deadline = time.monotonic() + timeout
1075|
1076|    if not timed_out:
1077|        try:
1078|            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
1079|        except subprocess.TimeoutExpired:
1080|            timed_out = True
1081|
1082|    if timed_out:
1083|        _terminate_command_stt_process_tree(proc)
1084|        for reader in readers:
1085|            reader.join(timeout=0.5)
1086|        while True:
1087|            try:
1088|                name, chunk = output_queue.get_nowait()
1089|            except queue.Empty:
1090|                break
1091|            if chunk:
1092|                chunks[name].append(chunk)
1093|        stdout = "".join(chunks["stdout"])
1094|        stderr = "".join(chunks["stderr"])
1095|        try:
1096|            raise subprocess.TimeoutExpired(command, timeout)
1097|        except subprocess.TimeoutExpired as exc:
1098|            raise subprocess.TimeoutExpired(
1099|                command,
1100|                timeout,
1101|                output=stdout,
1102|                stderr=stderr,
1103|            ) from exc
1104|
1105|    stdout = "".join(chunks["stdout"])
1106|    stderr = "".join(chunks["stderr"])
1107|
1108|    if proc.returncode:
1109|        raise subprocess.CalledProcessError(
1110|            proc.returncode,
1111|            command,
1112|            output=stdout,
1113|            stderr=stderr,
1114|        )
1115|    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
1116|
1117|
1118|def _read_command_stt_output(output_path: Path, stdout: str, fmt: str) -> str:
1119|    """Return the transcript text from a command-provider invocation.
1120|
1121|    Resolution:
1122|      1. If ``output_path`` exists and is non-empty → read it (raw text).
1123|      2. Else if ``stdout`` is non-empty → use stdout (lets users write
1124|         curl-style one-liners that emit transcript to stdout instead of
1125|         writing a file).
1126|      3. Else → raise RuntimeError (no usable output produced).
1127|
1128|    For JSON format, we still return the raw bytes — extracting a
1129|    ``text`` field is out of scope; users either configure ``format: txt``
1130|    or post-process JSON downstream. (Same trade-off as TTS: the runner
1131|    doesn't try to be clever about output shape.)
1132|    """
1133|    if output_path.exists():
1134|        try:
1135|            content = output_path.read_text(encoding="utf-8").strip()
1136|        except UnicodeDecodeError:
1137|            content = output_path.read_bytes().decode("utf-8", errors="replace").strip()
1138|        if content:
1139|            return content
1140|    if stdout and stdout.strip():
1141|        return stdout.strip()
1142|    raise RuntimeError(
1143|        f"Command STT provider wrote no output file at {output_path} "
1144|        f"and produced no stdout"
1145|    )
1146|
1147|
1148|def _transcribe_command_stt(
1149|    file_path: str,
1150|    provider_name: str,
1151|    config: Dict[str, Any],
1152|    stt_config: Dict[str, Any],
1153|    model_override: Optional[str] = None,
1154|    language_override: Optional[str] = None,
1155|    prompt: Optional[str] = None,
1156|) -> Dict[str, Any]:
1157|    """Transcribe via a user-declared ``stt.providers.<name>: type: command``.
1158|
1159|    Placeholder grammar:
1160|
1161|    | Placeholder       | Substituted with                                          |
1162|    |-------------------|-----------------------------------------------------------|
1163|    | ``{input_path}``  | absolute path to the audio file (original location)       |
1164|    | ``{output_path}`` | absolute path the provider should write its transcript to |
1165|    | ``{output_dir}``  | parent dir of ``{output_path}``                           |
1166|    | ``{format}``      | configured output format (``txt`` / ``json`` / ``srt`` / ``vtt``) |
1167|    | ``{language}``    | configured language code (default ``en``)                 |
1168|    | ``{model}``       | configured model id (empty when not set)                  |
1169|
1170|    All placeholders are shell-quote-aware (see ``_render_command_stt_template``).
1171|    Doubled braces ``{{`` and ``}}`` are preserved as literal braces.
1172|
1173|    Returns the standard transcribe-response envelope (``success``,
1174|    ``transcript``, ``provider``, ``error``).
1175|    """
1176|    if prompt:
1177|        logger.debug(
1178|            "Command STT provider '%s' does not support transcription "
1179|            "prompts — proceeding without the prompt.", provider_name,
1180|        )
1181|
1182|    command_template = str(config.get("command") or "").strip()
1183|    if not command_template:
1184|        return {
1185|            "success": False,
1186|            "transcript": "",
1187|            "provider": provider_name,
1188|            "error": f"stt.providers.{provider_name}.command is not configured",
1189|        }
1190|
1191|    audio = Path(file_path).expanduser()
1192|    if not audio.exists():
1193|        return {
1194|            "success": False,
1195|            "transcript": "",
1196|            "provider": provider_name,
1197|            "error": f"Audio file not found: {file_path}",
1198|        }
1199|
1200|    timeout = _get_command_stt_timeout(config)
1201|    output_format = _get_command_stt_output_format(config)
1202|    language = (
1203|        language_override
1204|        or config.get("language")
1205|        or _resolve_stt_language(provider_name, stt_config)
1206|        or DEFAULT_COMMAND_STT_LANGUAGE
1207|    )
1208|    model = model_override or config.get("model") or ""
1209|
1210|    try:
1211|        with tempfile.TemporaryDirectory(prefix=f"hermes-cmd-stt-{provider_name}-") as tmpdir:
1212|            output_path = Path(tmpdir) / f"transcript.{output_format}"
1213|            placeholders = {
1214|                "input_path": str(audio.resolve()),
1215|                "output_path": str(output_path),
1216|                "output_dir": str(output_path.parent),
1217|                "format": output_format,
1218|                "language": str(language),
1219|                "model": str(model),
1220|            }
1221|            command = _render_command_stt_template(command_template, placeholders)
1222|            logger.info(
1223|                "Transcribing %s via command STT provider '%s'...",
1224|                audio.name, provider_name,
1225|            )
1226|            try:
1227|                result = _run_command_stt(
1228|                    command,
1229|                    timeout,
1230|                    env_passthrough=_command_stt_env_passthrough(config),
1231|                )
1232|            except subprocess.TimeoutExpired:
1233|                return {
1234|                    "success": False,
1235|                    "transcript": "",
1236|                    "provider": provider_name,
1237|                    "error": (
1238|                        f"STT command provider '{provider_name}' timed out after "
1239|                        f"{timeout:g}s"
1240|                    ),
1241|                }
1242|            except subprocess.CalledProcessError as exc:
1243|                detail_parts = []
1244|                if exc.stderr:
1245|                    detail_parts.append(f"stderr: {exc.stderr.strip()}")
1246|                if exc.stdout:
1247|                    detail_parts.append(f"stdout: {exc.stdout.strip()}")
1248|                detail = "; ".join(detail_parts) or "no command output"
1249|                return {
1250|                    "success": False,
1251|                    "transcript": "",
1252|                    "provider": provider_name,
1253|                    "error": (
1254|                        f"STT command provider '{provider_name}' exited with code "
1255|                        f"{exc.returncode}: {detail}"
1256|                    ),
1257|                }
1258|
1259|            try:
1260|                transcript_text = _read_command_stt_output(
1261|                    output_path, result.stdout or "", output_format,
1262|                )
1263|            except RuntimeError as exc:
1264|                return {
1265|                    "success": False,
1266|                    "transcript": "",
1267|                    "provider": provider_name,
1268|                    "error": str(exc),
1269|                }
1270|
1271|    except OSError as exc:
1272|        return {
1273|            "success": False,
1274|            "transcript": "",
1275|            "provider": provider_name,
1276|            "error": f"STT command provider '{provider_name}' failed: {exc}",
1277|        }
1278|
1279|    logger.info(
1280|        "Transcribed %s via command STT provider '%s' (%d chars)",
1281|        audio.name, provider_name, len(transcript_text),
1282|    )
1283|    return {
1284|        "success": True,
1285|        "transcript": transcript_text,
1286|        "provider": provider_name,
1287|    }
1288|
1289|
1290|def _get_provider(stt_config: dict) -> str:
1291|    """Determine which STT provider to use.
1292|
1293|    When ``stt.provider`` is explicitly set in config, that choice is
1294|    honoured — no silent cloud fallback.  When no provider is configured,
1295|    auto-detect tries: local > groq (free) > openai (paid).
1296|    """
1297|    if not is_stt_enabled(stt_config):
1298|        return "none"
1299|
1300|    explicit = "provider" in stt_config
1301|    provider = stt_config.get("provider", DEFAULT_PROVIDER)
1302|
1303|    # The managed "Nous Subscription" selection (stt.provider: nous) is
1304|    # serviced by the OpenAI provider implementation, routed through the
1305|    # managed openai-audio gateway by _resolve_openai_audio_client_config.
1306|    if isinstance(provider, str) and provider.strip().lower() == "nous":
1307|        provider = "openai"
1308|
1309|    if explicit and provider == "local":
1310|        # Legacy DEFAULT_CONFIG seeded ``stt.provider: local`` on every
1311|        # install, so a merged-config "local" is not proof of a user pick.
1312|        # ``read_selection`` reads the raw config.yaml: when the raw file
1313|        # holds an stt selection (picker- or hand-written ``local``) it is
1314|        # honored; when the merged "local" came only from a legacy default
1315|        # merge, take the autodetect branch (which prefers local first
1316|        # anyway, so a genuine local user is unaffected when it's available).
1317|        try:
1318|            from tools.tool_backend_helpers import read_selection
1319|
1320|            if read_selection("stt") is None:
1321|                explicit = False
1322|        except Exception:  # pragma: no cover — helpers are in-repo
1323|            pass
1324|
1325|    # --- Explicit provider: respect the user's choice ----------------------
1326|
1327|    if explicit:
1328|        if provider == "local":
1329|            if _HAS_FASTER_WHISPER:
1330|                return "local"
1331|            if _has_local_command():
1332|                return "local_command"
1333|            # Try lazy-install before giving up
1334|            if _try_lazy_install_stt():
1335|                return "local"
1336|            logger.warning(
1337|                "STT provider 'local' configured but unavailable "
1338|                "(install faster-whisper or set HERMES_LOCAL_STT_COMMAND)"
1339|            )
1340|            return "none"
1341|
1342|        if provider == "local_command":
1343|            if _has_local_command():
1344|                return "local_command"
1345|            if _HAS_FASTER_WHISPER:
1346|                logger.info("Local STT command unavailable, using local faster-whisper")
1347|                return "local"
1348|            logger.warning(
1349|                "STT provider 'local_command' configured but unavailable"
1350|            )
1351|            return "none"
1352|
1353|        if provider == "groq":
1354|            if _HAS_OPENAI and _resolve_provider_key("GROQ_API_KEY", "groq"):
1355|                return "groq"
1356|            logger.warning(
1357|                "STT provider 'groq' configured but GROQ_API_KEY not set"
1358|            )
1359|            return "none"
1360|
1361|        if provider == "openai":
1362|            if _HAS_OPENAI and _has_openai_audio_backend():
1363|                return "openai"
1364|            logger.warning(
1365|                "STT provider 'openai' configured but no API key available"
1366|            )
1367|            return "none"
1368|
1369|        if provider == "mistral":
1370|            if _HAS_MISTRAL and _resolve_provider_key("MISTRAL_API_KEY", "mistral"):
1371|                return "mistral"
1372|            logger.warning(
1373|                "STT provider 'mistral' configured but mistralai package "
1374|                "not installed or MISTRAL_API_KEY not set"
1375|            )
1376|            return "none"
1377|
1378|        if provider == "xai":
1379|            from tools.xai_http import resolve_xai_http_credentials
1380|
1381|            if resolve_xai_http_credentials().get("api_key"):
1382|                return "xai"
1383|            logger.warning(
1384|                "STT provider 'xai' configured but no xAI credentials are available"
1385|            )
1386|            return "none"
1387|
1388|        if provider == "elevenlabs":
1389|            if _resolve_provider_key("ELEVENLABS_API_KEY", "elevenlabs"):
1390|                return "elevenlabs"
1391|            logger.warning(
1392|                "STT provider 'elevenlabs' configured but ELEVENLABS_API_KEY not set"
1393|            )
1394|            return "none"
1395|
1396|        if provider == "deepinfra":
1397|            if _HAS_OPENAI and _resolve_provider_key("DEEPINFRA_API_KEY", "deepinfra"):
1398|                return "deepinfra"
1399|            logger.warning(
1400|                "STT provider 'deepinfra' configured but DEEPINFRA_API_KEY not set "
1401|                "(or openai package missing)"
1402|            )
1403|            return "none"
1404|
1405|        return provider  # Unknown — let it fail downstream
1406|
1407|    # --- Auto-detect (no explicit provider):
1408|    #     local > groq > openai > mistral > xai > elevenlabs > deepinfra ---
1409|    # DeepInfra is tried LAST so adding DEEPINFRA_API_KEY (commonly set for the
1410|    # chat surface) never silently displaces an existing xAI/ElevenLabs STT
1411|    # auto-selection; a DeepInfra-only box still resolves to it. mistral is
1412|    # intentionally skipped while `mistralai` is quarantined on PyPI (malicious
1413|    # 2.4.6 release on 2026-05-12).
1414|
1415|    if _HAS_FASTER_WHISPER:
1416|        return "local"
1417|    if _has_local_command():
1418|        return "local_command"
1419|    # Try lazy-install before falling through to cloud providers
1420|    if _try_lazy_install_stt():
1421|        return "local"
1422|    if _HAS_OPENAI and _resolve_provider_key("GROQ_API_KEY", "groq"):
1423|        logger.info("No local STT available, using Groq Whisper API")
1424|        return "groq"
1425|    if _HAS_OPENAI and _has_openai_audio_backend():
1426|        logger.info("No local STT available, using OpenAI Whisper API")
1427|        return "openai"
1428|    # Only auto-select Mistral if the SDK is already present — don't trigger a
1429|    # lazy-install during passive auto-detection. Explicit `provider: mistral`
1430|    # (above) does lazy-install on first transcription call.
1431|    if _HAS_MISTRAL and _resolve_provider_key("MISTRAL_API_KEY", "mistral"):
1432|        logger.info("No local STT available, using Mistral Voxtral Transcribe API")
1433|        return "mistral"
1434|    try:
1435|        from tools.xai_http import resolve_xai_http_credentials
1436|
1437|        if resolve_xai_http_credentials().get("api_key"):
1438|            logger.info("No local STT available, using xAI Grok STT API")
1439|            return "xai"
1440|    except Exception:
1441|        pass
1442|    if _resolve_provider_key("ELEVENLABS_API_KEY", "elevenlabs"):
1443|        logger.info("No local STT available, using ElevenLabs Scribe STT API")
1444|        return "elevenlabs"
1445|    if _HAS_OPENAI and _resolve_provider_key("DEEPINFRA_API_KEY", "deepinfra"):
1446|        logger.info("No local STT available, using DeepInfra Whisper API")
1447|        return "deepinfra"
1448|    return "none"
1449|
1450|
1451|def _unregistered_stt_provider_error(provider: str) -> Dict[str, Any]:
1452|    key = str(provider or "").strip()
1453|    return {
1454|        "success": False,
1455|        "transcript": "",
1456|        "provider": key,
1457|        "error_type": "provider_not_registered",
1458|        "error": (
1459|            f"stt.provider='{key}' is set but no built-in, command, or plugin "
1460|            "provider registered that name. Run `hermes plugins list` to see "
1461|            "installed STT plugins, or configure a command provider under "
1462|            f"`stt.providers.{key}.command`."
1463|        ),
1464|    }
1465|
1466|
1467|# ---------------------------------------------------------------------------
1468|# Plugin provider dispatch (issue follow-up to #30398 — STT pluggability)
1469|# ---------------------------------------------------------------------------
1470|
1471|
1472|def _dispatch_to_plugin_provider(
1473|    file_path: str,
1474|    provider: str,
1475|    stt_config: Optional[Dict[str, Any]] = None,
1476|    *,
1477|    model: Optional[str] = None,
1478|    language: Optional[str] = None,
1479|    prompt: Optional[str] = None,
1480|) -> Optional[Dict[str, Any]]:
1481|    """Route the call to a plugin-registered transcription provider, or
1482|    return None.
1483|
1484|    Returns the transcribe-response dict on dispatch, or ``None`` when no
1485|    plugin claimed the provider name.
1486|
1487|    Resolution invariants enforced here:
1488|
1489|    1. Built-in provider names short-circuit — never reach the plugin
1490|       registry. The caller (``transcribe_audio``) handles ``local``,
1491|       ``groq``, ``openai``, etc. via its existing elif chain; this
1492|       function defensively rejects those names so a plugin can't be
1493|       silently dispatched under a built-in name even if it somehow
1494|       slipped past the registry's built-in shadow guard.
1495|    2. Same-name command-type provider declared under
1496|       ``stt.providers.<name>: type: command`` wins over a plugin. The
1497|       caller short-circuits to the command runner before reaching us,
1498|       but we re-verify here so a refactor of the caller can't silently
1499|       break the invariant (matches TTS PR #17843 precedence rule).
1500|    3. Plugin dispatch fires only when ``provider`` matches a
1501|       registered :class:`TranscriptionProvider` whose ``name`` equals
1502|       the configured value. Unknown names with no plugin registered
1503|       return None (caller surfaces the configured-provider error when
1504|       the name came from ``stt.provider``).
1505|    4. Availability gating: when the matched plugin reports
1506|       ``is_available() == False`` (missing API key, missing optional
1507|       SDK, etc.) this returns an error envelope identifying the
1508|       plugin as unavailable — **not** ``None`` — because the user
1509|       explicitly opted into this plugin via ``stt.provider`` and the
1510|       generic fallthrough message would be misleading.
1511|
1512|    Provider exceptions are caught and converted into the standard
1513|    error envelope (matches the legacy built-in error shapes — the
1514|    gateway/CLI caller already expects ``{success: False, error:
1515|    "...", transcript: ""}`` on failure).
1516|    """
1517|    if not provider:
1518|        return None
1519|    key = provider.lower().strip()
1520|    if key in BUILTIN_STT_PROVIDERS or key == "none":
1521|        return None
1522|    # Defense in depth: command-provider check should already have
1523|    # short-circuited the caller. If a same-name command config exists,
1524|    # bail so the command path wins.
1525|    if stt_config is not None and _is_command_stt_provider_config(
1526|        _get_named_stt_provider_config(stt_config, key)
1527|    ):
1528|        return None
1529|    try:
1530|        from agent.transcription_registry import get_provider
1531|        from hermes_cli.plugins import _ensure_plugins_discovered
1532|
1533|        _ensure_plugins_discovered()
1534|        plugin_provider = get_provider(key)
1535|        if plugin_provider is None:
1536|            # Long-lived sessions may have discovered plugins before a
1537|            # bundled backend was patched in or before config changed.
1538|            # Retry once with a forced refresh before surfacing fall-
1539|            # through. Mirrors the image_gen / browser dispatcher
1540|            # recovery pattern.
1541|            _ensure_plugins_discovered(force=True)
1542|            plugin_provider = get_provider(key)
1543|    except Exception as exc:  # noqa: BLE001 — discovery failure is non-fatal
1544|        logger.debug("STT plugin dispatch skipped (discovery failed): %s", exc)
1545|        return None
1546|    if plugin_provider is None:
1547|        return None
1548|
1549|    # Availability gate: when a plugin reports it's not configured
1550|    # (missing API key, missing optional SDK, etc.) surface a clean
1551|    # error envelope **instead of** falling through to the generic
1552|    # "No STT provider" message. The user explicitly set
1553|    # ``stt.provider: <plugin>`` in config — surfacing the plugin's
1554|    # own availability failure is more actionable than the generic
1555|    # auto-detect-failure error, and avoids routing the call into a
1556|    # plugin that's about to crash messily.
1557|    #
1558|    # ``is_available()`` MUST NOT raise per the ABC contract; defend
1559|    # anyway so a buggy plugin can't break dispatch for everyone.
1560|    try:
1561|        available = plugin_provider.is_available()
1562|    except Exception as exc:  # noqa: BLE001
1563|        logger.warning(
1564|            "STT plugin provider '%s' is_available() raised: %s — "
1565|            "treating as unavailable", key, exc, exc_info=True,
1566|        )
1567|        available = False
1568|    if not available:
1569|        logger.info(
1570|            "STT plugin provider '%s' reports not available; returning "
1571|            "unavailability envelope.", key,
1572|        )
1573|        return {
1574|            "success": False,
1575|            "transcript": "",
1576|            "error": (
1577|                f"STT plugin '{key}' is not available — check that its "
1578|                "required credentials / dependencies are configured."
1579|            ),
1580|            "provider": key,
1581|        }
1582|
1583|    logger.info("Transcribing with plugin STT provider '%s'...", key)
1584|    # Plugin providers receive the transcription prompt via the ABC's
1585|    # existing ``**extra`` kwargs — no signature change needed. The key is
1586|    # only sent when a prompt is actually set so providers that predate it
1587|    # see byte-identical calls on the no-prompt path.
1588|    extra_kwargs: Dict[str, Any] = {}
1589|    if prompt is not None:
1590|        extra_kwargs["prompt"] = prompt
1591|    try:
1592|        result = plugin_provider.transcribe(
1593|            file_path,
1594|            model=model,
1595|            language=language,
1596|            **extra_kwargs,
1597|        )
1598|    except Exception as exc:  # noqa: BLE001
1599|        logger.warning(
1600|            "STT plugin provider '%s' raised: %s", key, exc, exc_info=True,
1601|        )
1602|        return {
1603|            "success": False,
1604|            "transcript": "",
1605|            "error": f"STT plugin '{key}' raised: {exc}",
1606|            "provider": key,
1607|        }
1608|
1609|    # Defensive: plugins should return a dict matching the contract. If
1610|    # they don't, surface a clear error envelope rather than leaking a
1611|    # weird object back to the gateway.
1612|    if not isinstance(result, dict):
1613|        return {
1614|            "success": False,
1615|            "transcript": "",
1616|            "error": f"STT plugin '{key}' returned a non-dict result",
1617|            "provider": key,
1618|        }
1619|    # Stamp provider if the plugin forgot to.
1620|    result.setdefault("provider", key)
1621|    return result
1622|
1623|
1624|# ---------------------------------------------------------------------------
1625|# pre_transcription plugin hook (issue #64168 — STT prompt/vocab threading)
1626|# ---------------------------------------------------------------------------
1627|
1628|
1629|# Fields a pre_transcription hook may mutate. ``file_path`` is deliberately
1630|# absent — it is read-only; attempts to change it are logged and dropped.
1631|_PRE_TRANSCRIPTION_MUTABLE_FIELDS = ("prompt", "language", "model")
1632|
1633|# Whisper-family models silently use only the final ~224 tokens of the
1634|# prompt/initial_prompt; longer values waste upload bytes and can trip
1635|# stricter OpenAI-compatible servers. Enforce the cap client-side for the
1636|# whisper-family backends: truncate with a warning, never error.
1637|# Approximation: ~4 characters per token (no tokenizer dependency).
1638|_WHISPER_PROMPT_TOKEN_CAP = 224
1639|_PROMPT_CHARS_PER_TOKEN = 4
1640|# Providers whose prompt parameter feeds a whisper-family model.
1641|_WHISPER_PROMPT_CAPPED_PROVIDERS = frozenset(
1642|    {"local", "openai", "groq", "deepinfra"}
1643|)
1644|
1645|
1646|def _enforce_prompt_length_limit(
1647|    prompt: Optional[str], provider: str
1648|) -> Optional[str]:
1649|    """Truncate *prompt* to the provider's known token cap (fail-open).
1650|
1651|    Only whisper-family backends have a documented ~224-token prompt window;
1652|    other providers (mistral, plugin providers) own their own validation.
1653|    Truncation keeps the TAIL of the prompt because whisper conditions on
1654|    the final context window — the most recently appended hints survive.
1655|    """
1656|    if not prompt or provider not in _WHISPER_PROMPT_CAPPED_PROVIDERS:
1657|        return prompt
1658|    max_chars = _WHISPER_PROMPT_TOKEN_CAP * _PROMPT_CHARS_PER_TOKEN
1659|    if len(prompt) <= max_chars:
1660|        return prompt
1661|    logger.warning(
1662|        "Transcription prompt is ~%d tokens; whisper-family provider '%s' "
1663|        "only uses the final ~%d — truncating to the last %d characters.",
1664|        len(prompt) // _PROMPT_CHARS_PER_TOKEN,
1665|        provider,
1666|        _WHISPER_PROMPT_TOKEN_CAP,
1667|        max_chars,
1668|    )
1669|    return prompt[-max_chars:]
1670|
1671|
1672|def _apply_pre_transcription_hook(
1673|    *,
1674|    file_path: str,
1675|    provider: str,
1676|    model: Optional[str],
1677|    language: Optional[str],
1678|    prompt: Optional[str],
1679|    source: Optional[str],
1680|) -> tuple[Optional[str], Optional[str], Optional[str]]:
1681|    """Fire the ``pre_transcription`` plugin hook and merge its results.
1682|
1683|    Mirrors the ``transform_*`` hook mechanics (``transform_tool_result``):
1684|    gated on ``has_hook`` so the no-hook dispatch path never builds hook
1685|    kwargs, and fail-open — any hook-plumbing error leaves the dispatch
1686|    untouched. ``invoke_hook`` returns results in registration order, and
1687|    plugin discovery scans plugin directories in sorted order, so multiple
1688|    plugins' hints compose deterministically (sorted by plugin id, then
1689|    each plugin's own registration order). Each dict result is applied
1690|    field-by-field on top of the previous ones, so the last hook to write
1691|    a field wins (last-writer-wins per field).
1692|
1693|    Model values are accepted as-is: the dispatcher has no catalog-level
1694|    validation today, so a hook-set model flows through the exact same
1695|    per-backend normalization/auto-correction (``_normalize_local_model``,
1696|    the Groq/OpenAI cross-corrections) a caller-supplied model would, and
1697|    otherwise errors at the backend as it would today.
1698|
1699|    Returns ``(model, language_override, prompt)``. ``language_override``
1700|    is ``None`` unless a hook explicitly set ``language`` — backends keep
1701|    their existing config/env language resolution when no hook overrides
1702|    it.
1703|    """
1704|    try:
1705|        from hermes_cli.plugins import has_hook, invoke_hook
1706|
1707|        # No-hook short-circuit: keep the no-plugin dispatch path
1708|        # byte-identical (no kwargs built, no invoke_hook call).
1709|        if not has_hook("pre_transcription"):
1710|            return model, None, prompt
1711|
1712|        hook_results = invoke_hook(
1713|            "pre_transcription",
1714|            file_path=file_path,
1715|            provider=provider,
1716|            model=model,
1717|            language=language,
1718|            prompt=prompt,
1719|            source=source,
1720|        )
1721|        overrides: Dict[str, Any] = {}
1722|        for hook_result in hook_results:
1723|            if not isinstance(hook_result, dict):
1724|                continue
1725|            for key, value in hook_result.items():
1726|                if key == "file_path":
1727|                    # file_path is read-only for hooks — log and drop.
1728|                    logger.warning(
1729|                        "pre_transcription hook attempted to change "
1730|                        "file_path (read-only) — ignoring the attempt."
1731|                    )
1732|                    continue
1733|                if key not in _PRE_TRANSCRIPTION_MUTABLE_FIELDS:
1734|                    logger.debug(
1735|                        "pre_transcription hook returned unsupported field "
1736|                        "%r — ignoring.", key,
1737|                    )
1738|                    continue
1739|                if not isinstance(value, str):
1740|                    logger.debug(
1741|                        "pre_transcription hook returned non-string value "
1742|                        "%r for field %r — ignoring.", value, key,
1743|                    )
1744|                    continue
1745|                overrides[key] = value
1746|
1747|        if "model" in overrides:
1748|            model = overrides["model"]
1749|        if "prompt" in overrides:
1750|            # Hook results win over the static ``stt.prompt`` config value —
1751|            # config is the base, hooks mutate on top. An empty string
1752|            # clears the config prompt.
1753|            prompt = overrides["prompt"] or None
1754|        return model, overrides.get("language") or None, prompt
1755|    except Exception as _hook_err:  # noqa: BLE001 — hook plumbing is fail-open
1756|        logger.debug("pre_transcription hook error: %s", _hook_err)
1757|        return model, None, prompt
1758|
1759|
1760|# ---------------------------------------------------------------------------
1761|# Shared validation
1762|# ---------------------------------------------------------------------------
1763|
1764|
1765|def _validate_audio_file_size(audio_path: Path) -> Optional[Dict[str, Any]]:
1766|    """Return an error when *audio_path* exceeds the remote upload cap."""
1767|    try:
1768|        file_size = audio_path.stat().st_size
1769|    except OSError as e:
1770|        return {"success": False, "transcript": "", "error": f"Failed to access file: {e}"}
1771|    if file_size > MAX_FILE_SIZE:
1772|        return {
1773|            "success": False,
1774|            "transcript": "",
1775|            "error": f"File too large: {file_size / (1024*1024):.1f}MB (max {MAX_FILE_SIZE / (1024*1024):.0f}MB)",
1776|        }
1777|    return None
1778|
1779|
1780|def _validate_audio_source_file(
1781|    file_path: str,
1782|    *,
1783|    enforce_size_limit: bool = True,
1784|) -> Optional[Dict[str, Any]]:
1785|    """Validate source path safety (and optionally size) before any decoder runs."""
1786|    audio_path = Path(file_path)
1787|
1788|    if os.path.islink(audio_path):
1789|        return {"success": False, "transcript": "", "error": f"Path is a symbolic link: {file_path}"}
1790|    if not audio_path.exists():
1791|        return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
1792|    if not audio_path.is_file():
1793|        return {"success": False, "transcript": "", "error": f"Path is not a file: {file_path}"}
1794|    if enforce_size_limit:
1795|        return _validate_audio_file_size(audio_path)
1796|    try:
1797|        audio_path.stat()
1798|    except OSError as e:
1799|        return {"success": False, "transcript": "", "error": f"Failed to access file: {e}"}
1800|    return None
1801|
1802|
1803|def _validate_audio_file(
1804|    file_path: str,
1805|    *,
1806|    enforce_size_limit: bool = True,
1807|) -> Optional[Dict[str, Any]]:
1808|    """Validate a supported, decoder-safe audio file."""
1809|    source_error = _validate_audio_source_file(
1810|        file_path, enforce_size_limit=enforce_size_limit
1811|    )
1812|    if source_error:
1813|        return source_error
1814|
1815|    audio_path = Path(file_path)
1816|    if audio_path.suffix.lower() not in SUPPORTED_FORMATS:
1817|        return {
1818|            "success": False,
1819|            "transcript": "",
1820|            "error": f"Unsupported format: {audio_path.suffix}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
1821|        }
1822|    return None
1823|
1824|
1825|def _prepare_audio_for_transcription(
1826|    file_path: str,
1827|) -> tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
1828|    """Convert a decoder-safe .silk source to a temporary supported WAV file."""
1829|    audio_path = Path(file_path)
1830|    if audio_path.suffix.lower() != ".silk":
1831|        return file_path, None, None
1832|    if not _HAS_PILK:
1833|        # pilk is a tiny silk-v3 codec binding — lazy-install it on first
1834|        # .silk voice note instead of bloating the base install.
1835|        try:
1836|            from tools.lazy_deps import ensure as _lazy_ensure
1837|            _lazy_ensure("stt.silk", prompt=False)
1838|        except Exception:
1839|            pass
1840|        if not _safe_find_spec("pilk"):
1841|            return None, None, {
1842|                "success": False,
1843|                "transcript": "",
1844|                "error": "Unsupported format: .silk. Install the optional 'pilk' dependency to enable WeChat voice transcription.",
1845|            }
1846|
1847|    temp_dir = tempfile.mkdtemp(prefix="hermes-silk-")
1848|    converted_path = os.path.join(temp_dir, f"{audio_path.stem}.wav")
1849|    try:
1850|        import pilk
1851|
1852|        pilk.silk_to_wav(file_path, converted_path)
1853|        if not Path(converted_path).is_file() or Path(converted_path).stat().st_size == 0:
1854|            raise RuntimeError("pilk did not produce a readable WAV file")
1855|        return converted_path, temp_dir, None
1856|    except Exception as exc:
1857|        shutil.rmtree(temp_dir, ignore_errors=True)
1858|        logger.error("Failed to convert .silk audio %s: %s", file_path, exc, exc_info=True)
1859|        return None, None, {
1860|            "success": False,
1861|            "transcript": "",
1862|            "error": f"Failed to convert .silk audio for transcription: {exc}",
1863|        }
1864|
1865|# ---------------------------------------------------------------------------
1866|# Provider: local (faster-whisper)
1867|# ---------------------------------------------------------------------------
1868|
1869|
1870|# Substrings that identify a missing/unloadable CUDA runtime library.  When
1871|# ctranslate2 (the backend for faster-whisper) cannot dlopen one of these, the
1872|# "auto" device picker has already committed to CUDA and the model can no
1873|# longer be used — we fall back to CPU and reload.
1874|#
1875|# Deliberately narrow: we match on library-name tokens and dlopen phrasing so
1876|# we DO NOT accidentally catch legitimate runtime failures like "CUDA out of
1877|# memory" — those should surface to the user, not silently fall back to CPU
1878|# (a 32GB audio clip on CPU at int8 isn't useful either).
1879|_CUDA_LIB_ERROR_MARKERS = (
1880|    "libcublas",
1881|    "libcudnn",
1882|    "libcudart",
1883|    "cannot be loaded",
1884|    "cannot open shared object",
1885|    "no kernel image is available",
1886|    "CUBLAS_STATUS_NOT_SUPPORTED",
1887|    "no CUDA-capable device",
1888|    "CUDA driver version is insufficient",
1889|)
1890|
1891|
1892|def _looks_like_cuda_lib_error(exc: BaseException) -> bool:
1893|    """Heuristic: is this exception a missing/broken CUDA runtime library?
1894|
1895|    ctranslate2 raises plain RuntimeError with messages like
1896|    ``Library libcublas.so.12 is not found or cannot be loaded``.  We want to
1897|    catch missing/unloadable shared libs and driver-mismatch errors, NOT
1898|    legitimate runtime failures ("CUDA out of memory", model bugs, etc.).
1899|    """
1900|    msg = str(exc)
1901|    return any(marker in msg for marker in _CUDA_LIB_ERROR_MARKERS)
1902|
1903|
1904|def _sysctl_value(name: str) -> str:
1905|    """Return a sysctl value, or an empty string when unavailable."""
1906|    try:
1907|        return subprocess.check_output(
1908|            ["/usr/sbin/sysctl", "-n", name],
1909|            stderr=subprocess.DEVNULL,
1910|            text=True,
1911|            timeout=2,
1912|        ).strip()
1913|    except Exception:
1914|        return ""
1915|
1916|
1917|def _should_force_faster_whisper_cpu() -> bool:
1918|    """Avoid faster-whisper device autodetection paths known to hard-abort.
1919|
1920|    On Apple Silicon, especially when Python is running as x86_64 under
1921|    Rosetta, ctranslate2's ``device=\"auto\"`` path can abort inside native
1922|    code before Python can catch an exception.  Force CPU so local STT remains
1923|    reliable for gateway voice messages.
1924|    """
1925|    if platform.system() != "Darwin":
1926|        return False
1927|
1928|    machine = platform.machine().lower()
1929|    if machine in {"arm64", "aarch64"}:
1930|        return True
1931|
1932|    # Under Rosetta, platform.machine() reports x86_64.  sysctl.proc_translated
1933|    # tells us this process is translated, while hw.optional.arm64 distinguishes
1934|    # Apple Silicon hosts from Intel Macs.
1935|    if _sysctl_value("sysctl.proc_translated") == "1":
1936|        return True
1937|    return _sysctl_value("hw.optional.arm64") == "1"
1938|
1939|
1940|def _get_idle_unload_seconds(local_cfg: Dict[str, Any]) -> int:
1941|    """Resolve the idle unload timeout from config.
1942|
1943|    0 = never unload (default). Negative values are treated as 0.
1944|    """
1945|    try:
1946|        val = int(local_cfg.get("unload_after_idle_seconds", 0))
1947|    except (TypeError, ValueError):
1948|        return 0
1949|    return max(val, 0)
1950|
1951|
1952|def _unload_local_model() -> None:
1953|    """Release the cached local whisper model and free its memory.
1954|
1955|    Safe to call from any thread. The model lock prevents races with a
1956|    concurrent transcription that is mid-load.
1957|    """
1958|    global _local_model, _local_model_name
1959|    with _local_model_lock:
1960|        if _local_model is not None:
1961|            logger.info(
1962|                "Unloading local whisper model '%s' after idle timeout",
1963|                _local_model_name or "unknown",
1964|            )
1965|            _local_model = None
1966|            _local_model_name = None
1967|
1968|
1969|def _start_idle_unload_watcher(timeout_seconds: int) -> None:
1970|    """Ensure the idle-unload watcher thread is running.
1971|
1972|    A single long-lived watcher: started only when none is alive, so the
1973|    per-transcription cost is one lock + one ``is_alive()`` check — no
1974|    stop/join/restart churn on the response path. The loop re-reads the
1975|    configured timeout from config every cycle, so changing
1976|    ``stt.local.unload_after_idle_seconds`` takes effect within one check
1977|    interval without a restart. After unloading (or when the timeout is set
1978|    to 0/never, or the model is already gone) the thread exits; the next
1979|    transcription restarts it.
1980|
1981|    ``timeout_seconds`` seeds the first cycle so a just-written config is
1982|    honored even if a concurrent config read would race.
1983|    """
1984|    global _idle_unload_thread
1985|    with _idle_unload_mgmt_lock:
1986|        if _idle_unload_thread is not None and _idle_unload_thread.is_alive():
1987|            return
1988|
1989|        def _watch(initial_timeout=timeout_seconds):
1990|            timeout = initial_timeout
1991|            while not _idle_unload_stop.is_set():
1992|                if _idle_unload_stop.wait(_IDLE_UNLOAD_CHECK_INTERVAL):
1993|                    break
1994|                if _local_model is None:
1995|                    break
1996|                # Re-read the timeout each cycle: config edits apply without
1997|                # waiting for the next voice message.
1998|                try:
1999|                    timeout = _get_idle_unload_seconds(
2000|                        _load_stt_config().get("local") or {}
2001|