"""Per-guild Discord music queues and playback controls.

Streaming is intentionally resolved at playback time so expiring media URLs are
never persisted. Spotify links contribute metadata only; audio is resolved from
a supported source by yt-dlp rather than attempting to bypass Spotify DRM.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Iterable, Optional
from urllib.parse import urlparse


class MusicResolutionError(RuntimeError):
    """A user-facing media-resolution failure."""


class TrackResolver:
    """Turn user input into queue metadata and fresh playable stream URLs."""

    MAX_TRACKS_PER_REQUEST = 25
    TRUSTED_STREAM_HOSTS = (
        "googlevideo.com",
        "youtube.com",
        "sndcdn.com",
        "soundcloud.com",
        "bcbits.com",
        "bandcamp.com",
        "vimeocdn.com",
        "akamaized.net",
        "twitchcdn.net",
        "jtvnw.net",
        "mixcloud.com",
        "dmcdn.net",
        "dailymotion.com",
    )

    def __init__(
        self,
        *,
        spotify_metadata: Optional[Callable[[str], list[dict]]] = None,
        media_extractor: Optional[Callable[..., dict]] = None,
        http_get: Optional[Callable[..., object]] = None,
        url_guard: Optional[Callable[[str], bool]] = None,
        url_support_checker: Optional[Callable[[str], bool]] = None,
    ):
        if url_guard is None:
            from tools.url_safety import is_safe_url

            url_guard = is_safe_url
        self._url_guard = url_guard
        self._url_support_checker = url_support_checker or self._has_supported_extractor
        self._http_get = http_get or self._default_http_get
        self._spotify_metadata = spotify_metadata or self._fetch_spotify_metadata
        self._media_extractor = media_extractor or self._extract_media

    @staticmethod
    def _is_spotify_url(value: str) -> bool:
        host = (urlparse(value).hostname or "").lower()
        return host in {"open.spotify.com", "spotify.link"} or host.endswith(
            ".spotify.com"
        )

    @staticmethod
    def _is_youtube_lookup(value: str) -> bool:
        host = (urlparse(value).hostname or "").lower()
        return bool(
            re.match(r"^ytsearch\d*:", value)
            or host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
        )

    def resolve(
        self,
        query: str,
        *,
        requester_id: int,
        requester_name: str,
    ) -> list["MusicTrack"]:
        query = str(query or "").strip()
        if not query:
            raise MusicResolutionError("Provide a song name or supported music link.")
        parsed = urlparse(query)
        if parsed.scheme in {"http", "https"} and not self._url_guard(query):
            raise MusicResolutionError(
                "For safety, private or local media URLs are not allowed."
            )
        if self._is_spotify_url(query):
            path_parts = [part for part in parsed.path.split("/") if part]
            if parsed.hostname == "open.spotify.com" and (
                not path_parts or path_parts[0] != "track"
            ):
                raise MusicResolutionError(
                    "Only Spotify track links are currently supported; albums and playlists need Spotify API metadata access."
                )
            items = self._spotify_metadata(query)
            tracks = []
            for item in items[: self.MAX_TRACKS_PER_REQUEST]:
                title = str(item.get("title") or "Unknown track").strip()
                artist = str(item.get("artist") or "").strip()
                display = f"{title} — {artist}" if artist else title
                search = " ".join(part for part in (title, artist, "audio") if part)
                tracks.append(
                    MusicTrack(
                        title=display,
                        webpage_url=str(item.get("webpage_url") or query),
                        lookup=f"ytsearch5:{search}",
                        requester_id=int(requester_id),
                        requester_name=str(requester_name),
                        duration=item.get("duration"),
                        thumbnail=item.get("thumbnail"),
                    )
                )
            if not tracks:
                raise MusicResolutionError(
                    "No playable tracks were found in that Spotify link."
                )
            return tracks

        if parsed.scheme in {"http", "https"} and not self._url_support_checker(query):
            raise MusicResolutionError(
                "That URL is not from a supported streaming platform. Try a song name, Spotify, YouTube, or another yt-dlp-supported service."
            )
        is_direct_url = parsed.scheme in {"http", "https"}
        lookup = query if is_direct_url else f"ytsearch5:{query}"
        info = self._media_extractor(lookup, flat=True)
        entries = info.get("entries") if isinstance(info, dict) else None
        if entries is None:
            entries = [info]
        elif not is_direct_url:
            # A text search queues one requested song, not every fallback
            # candidate. Keep the broader search lookup on that track so
            # playback can skip YouTube results without a usable HLS stream.
            entries = list(entries)[:1]
        tracks = []
        for item in list(entries)[: self.MAX_TRACKS_PER_REQUEST]:
            if not isinstance(item, dict):
                continue
            webpage_url = str(item.get("webpage_url") or item.get("url") or lookup)
            tracks.append(
                MusicTrack(
                    title=str(item.get("title") or "Unknown track"),
                    webpage_url=webpage_url,
                    lookup=webpage_url if is_direct_url else lookup,
                    requester_id=int(requester_id),
                    requester_name=str(requester_name),
                    duration=item.get("duration"),
                    thumbnail=item.get("thumbnail"),
                )
            )
        if not tracks:
            raise MusicResolutionError(
                "No playable tracks were found for that request."
            )
        return tracks

    def resolve_stream(self, track: "MusicTrack") -> str:
        info = self._media_extractor(track.lookup, flat=False)
        if isinstance(info, dict) and info.get("entries"):
            info = next((item for item in info["entries"] if item), {})
        stream_url = str((info or {}).get("url") or "")
        if not stream_url:
            raise MusicResolutionError(
                f"Could not obtain an audio stream for {track.title}."
            )
        parsed_stream = urlparse(stream_url)
        if parsed_stream.scheme not in {"http", "https"}:
            raise MusicResolutionError("Resolved audio streams must use HTTP or HTTPS.")
        stream_host = (parsed_stream.hostname or "").lower()
        protocol = str((info or {}).get("protocol") or "").lower()
        if (
            protocol.startswith("m3u8")
            and self._is_youtube_lookup(track.lookup)
            and stream_host != "manifest.googlevideo.com"
        ):
            raise MusicResolutionError(
                "The resolved HLS stream is not on YouTube's trusted manifest host."
            )
        if not any(
            stream_host == host or stream_host.endswith(f".{host}")
            for host in self.TRUSTED_STREAM_HOSTS
        ):
            raise MusicResolutionError(
                "The resolved audio stream is not on a trusted media host."
            )
        if not self._url_guard(stream_url):
            raise MusicResolutionError(
                "The resolved audio stream points to a private or local address."
            )
        return stream_url

    @staticmethod
    def _has_supported_extractor(url: str) -> bool:
        try:
            from yt_dlp.extractor import gen_extractor_classes
        except ImportError as exc:
            raise MusicResolutionError(
                "Music playback requires yt-dlp. Reinstall Hermes with messaging support."
            ) from exc
        for extractor_cls in gen_extractor_classes():
            try:
                if extractor_cls.suitable(url):
                    return (
                        str(getattr(extractor_cls, "IE_NAME", "")).lower() != "generic"
                    )
            except Exception:
                continue
        return False

    @staticmethod
    def _extract_media(query: str, *, flat: bool) -> dict:
        try:
            import yt_dlp
        except ImportError as exc:
            raise MusicResolutionError(
                "Music playback requires yt-dlp. Reinstall Hermes with messaging support."
            ) from exc
        is_youtube = TrackResolver._is_youtube_lookup(query)
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "extract_flat": "in_playlist" if flat else False,
            "playlistend": TrackResolver.MAX_TRACKS_PER_REQUEST,
            # YouTube's direct GVS URLs can return HTTP 403 even when freshly
            # extracted. Require its lowest-bandwidth HLS stream and discard
            # the video in FFmpeg. Other providers retain audio-only formats.
            "format": (
                None
                if flat
                else (
                    "worst[protocol^=m3u8][acodec!=none]"
                    if is_youtube
                    else "bestaudio/best"
                )
            ),
            # Search several candidates and let yt-dlp omit YouTube results
            # that do not expose a playable HLS stream.
            "ignoreerrors": is_youtube,
            # yt-dlp enables Deno by default but not Node. Hermes installations
            # already ship Node, so allow either supported runtime to solve
            # YouTube's JavaScript challenges.
            "js_runtimes": {
                "deno": {"path": None},
                "node": {"path": None},
            },
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                result = ydl.extract_info(query, download=False)
        except Exception as exc:
            raise MusicResolutionError(
                f"Could not resolve that music source: {exc}"
            ) from exc
        return result or {}

    @staticmethod
    def _default_http_get(url: str, **kwargs):
        from tools.url_safety import create_ssrf_safe_client

        with create_ssrf_safe_client(**kwargs) as client:
            return client.get(url)

    def _fetch_spotify_metadata(self, url: str) -> list[dict]:
        try:
            page = self._http_get(
                url,
                follow_redirects=True,
                timeout=15.0,
                headers={"User-Agent": "Hermes-Agent music queue"},
            )
            page.raise_for_status()
            canonical_url = str(getattr(page, "url", None) or url)
            canonical = urlparse(canonical_url)
            canonical_parts = [part for part in canonical.path.split("/") if part]
            if (
                canonical.hostname != "open.spotify.com"
                or not canonical_parts
                or canonical_parts[0] != "track"
            ):
                raise MusicResolutionError(
                    "Only Spotify track links are currently supported; albums and playlists need Spotify API metadata access."
                )
            oembed = self._http_get(
                "https://open.spotify.com/oembed",
                params={"url": canonical_url},
                follow_redirects=True,
                timeout=15.0,
            )
            oembed.raise_for_status()
            embed_data = oembed.json() or {}
        except Exception as exc:
            raise MusicResolutionError(
                f"Spotify metadata could not be resolved: {exc}"
            ) from exc

        description_match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]+content=["\']([^"\']+)',
            str(getattr(page, "text", "")),
            re.IGNORECASE,
        )
        description = (
            html.unescape(description_match.group(1)) if description_match else ""
        )
        parts = [part.strip() for part in description.split("·") if part.strip()]
        title = str(
            embed_data.get("title")
            or (parts[1] if len(parts) > 1 else parts[0] if parts else "Spotify track")
        )
        artist = parts[0] if len(parts) > 1 else ""
        return [
            {
                "title": title,
                "artist": artist,
                "webpage_url": canonical_url,
                "thumbnail": embed_data.get("thumbnail_url"),
            }
        ]


@dataclass(slots=True)
class MusicTrack:
    title: str
    webpage_url: str
    lookup: str
    requester_id: int
    requester_name: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None


@dataclass
class MusicSession:
    guild_id: int
    queue: Deque[MusicTrack] = field(default_factory=deque)
    history: Deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=50))
    current: Optional[MusicTrack] = None
    repeat_current: bool = False
    playback_generation: int = 0
    last_error: Optional[str] = None
    text_channel: object = None
    panel_message: object = None

    def enqueue(self, tracks: Iterable[MusicTrack]) -> None:
        self.queue.extend(tracks)

    def can_control(self, *, user_id: int, administrator: bool) -> bool:
        return bool(
            administrator
            or (self.current is not None and self.current.requester_id == int(user_id))
        )

    def render_queue(self) -> str:
        lines = []
        if self.current:
            lines.append(
                f"Now playing: **{self.current.title}** — requested by "
                f"{self.current.requester_name}"
            )
        else:
            lines.append("Nothing is playing.")
        if self.queue:
            lines.append("\nUp next:")
            for index, track in enumerate(list(self.queue)[:10], start=1):
                lines.append(
                    f"{index}. **{track.title}** — requested by {track.requester_name}"
                )
            remaining = len(self.queue) - 10
            if remaining > 0:
                lines.append(f"…and {remaining} more")
        else:
            lines.append("\nQueue is empty.")
        if self.repeat_current:
            lines.append("\nRepeat: current song")
        if self.last_error:
            lines.append(f"\n⚠️ {self.last_error}")
        return "\n".join(lines)


logger = logging.getLogger(__name__)


class DiscordMusicManager:
    """Own one visible FIFO player per Discord guild."""

    def __init__(
        self,
        adapter,
        *,
        resolver: Optional[TrackResolver] = None,
        audio_source_factory: Optional[Callable[[str], object]] = None,
        view_factory: Optional[Callable[["DiscordMusicManager", int], object]] = None,
    ) -> None:
        self.adapter = adapter
        self.resolver = resolver or TrackResolver()
        self.sessions: dict[int, MusicSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._audio_source_factory = audio_source_factory or self._default_audio_source
        self._view_factory = view_factory or (
            lambda manager, guild_id: MusicControlView(manager, guild_id)
        )

    @staticmethod
    def _default_audio_source(stream_url: str):
        import discord

        from plugins.platforms.discord.ffmpeg_utils import resolve_ffmpeg_executable

        return discord.FFmpegPCMAudio(
            stream_url,
            executable=resolve_ffmpeg_executable(),
            before_options=(
                "-protocol_whitelist http,https,tcp,tls,crypto "
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
            ),
            options="-vn -loglevel warning",
        )

    async def add(self, interaction, query: str) -> None:
        guild = getattr(interaction, "guild", None)
        if guild is None:
            await interaction.response.send_message(
                "Music playback is only available in a server.", ephemeral=True
            )
            return
        voice_state = getattr(getattr(interaction, "user", None), "voice", None)
        voice_channel = getattr(voice_state, "channel", None)
        if voice_channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then run `/play` again.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        tracks = await asyncio.to_thread(
            self.resolver.resolve,
            query,
            requester_id=int(interaction.user.id),
            requester_name=str(
                getattr(interaction.user, "display_name", None)
                or getattr(interaction.user, "name", interaction.user.id)
            ),
        )
        joined = await self.adapter.join_voice_channel(
            voice_channel,
            text_channel_id=getattr(interaction.channel, "id", None),
        )
        if not joined:
            raise MusicResolutionError("I could not join your voice channel.")

        guild_id = int(guild.id)
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            session = self.sessions.setdefault(
                guild_id, MusicSession(guild_id=guild_id)
            )
            session.text_channel = interaction.channel
            session.enqueue(tracks)
            await self._update_panel(session)
            if session.current is None:
                await self._start_next(session)

        label = tracks[0].title if len(tracks) == 1 else f"{len(tracks)} tracks"
        await interaction.followup.send(
            f"Added **{label}** to the music queue.", ephemeral=True
        )

    async def _update_panel(self, session: MusicSession) -> None:
        import discord

        content = session.render_queue()
        view = self._view_factory(self, session.guild_id)
        allowed_mentions_cls = discord.AllowedMentions
        none_factory = getattr(allowed_mentions_cls, "none", None)
        if callable(none_factory):
            allowed_mentions = none_factory()
        else:
            # Some Discord-compatible adapters expose the constructor but not
            # discord.py's convenience classmethod. Preserve fail-closed mention
            # behavior across both forms.
            allowed_mentions = allowed_mentions_cls(
                everyone=False,
                roles=False,
                users=False,
                replied_user=False,
            )
        if session.panel_message is None:
            session.panel_message = await session.text_channel.send(
                content, view=view, allowed_mentions=allowed_mentions
            )
        else:
            await session.panel_message.edit(
                content=content, view=view, allowed_mentions=allowed_mentions
            )

    async def _start_next(self, session: MusicSession) -> None:
        if session.current is not None:
            return
        while session.queue and session.current is None:
            track = session.queue.popleft()
            session.current = track
            source = None
            try:
                stream_url = await asyncio.to_thread(
                    self.resolver.resolve_stream, track
                )
                source = self._audio_source_factory(stream_url)
                vc = self.adapter._voice_clients.get(session.guild_id)
                if vc is None or not vc.is_connected():
                    raise MusicResolutionError("The voice connection was lost.")
                self.adapter._cancel_voice_timeout(session.guild_id)
                receiver = self.adapter._voice_receivers.get(session.guild_id)
                if receiver:
                    receiver.pause()
                self.adapter._voice_mixers.pop(session.guild_id, None)
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                loop = asyncio.get_running_loop()
                session.playback_generation += 1
                generation = session.playback_generation

                def _after(error):
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            self._on_track_end(
                                session.guild_id,
                                error,
                                generation=generation,
                            )
                        )
                    )

                vc.play(source, after=_after)
                session.last_error = None
                await self._update_panel(session)
                return
            except Exception as exc:
                logger.warning(
                    "Skipping unplayable Discord music track %r in guild %s: %s",
                    track.title,
                    session.guild_id,
                    exc,
                )
                if source is not None and hasattr(source, "cleanup"):
                    source.cleanup()
                session.last_error = f"Skipped **{track.title}**: {exc}"
                session.current = None
                await self._update_panel(session)
        if session.current is None:
            await self._restore_voice_listening(session.guild_id)

    async def _reply(self, interaction, message: str) -> None:
        is_done = getattr(getattr(interaction, "response", None), "is_done", None)
        if callable(is_done) and is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def show_queue(self, interaction, guild_id: int) -> None:
        """Acknowledge, then serialize the public panel refresh for a guild."""
        guild_id = int(guild_id)
        await interaction.response.defer(ephemeral=True)
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            session = self.sessions.get(guild_id)
            if session is None:
                await interaction.followup.send(
                    "The music queue is empty.", ephemeral=True
                )
                return
            session.text_channel = interaction.channel
            await self._update_panel(session)
        await interaction.followup.send(
            "The public music queue is up to date.", ephemeral=True
        )

    async def control(self, interaction, guild_id: int, action: str) -> None:
        guild_id = int(guild_id)
        if hasattr(interaction.response, "defer"):
            await interaction.response.defer(ephemeral=True)
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            session = self.sessions.get(guild_id)
            administrator = bool(
                getattr(
                    getattr(interaction.user, "guild_permissions", None),
                    "administrator",
                    False,
                )
            )
            if session is None or not session.can_control(
                user_id=int(interaction.user.id), administrator=administrator
            ):
                await self._reply(
                    interaction,
                    "Only the requester of the current song or a server administrator can use these controls.",
                )
                return
            vc = self.adapter._voice_clients.get(guild_id)
            if action == "skip":
                if vc is None:
                    await self._reply(interaction, "The voice connection was lost.")
                    return
                vc.stop()
                await self._reply(interaction, "Skipped.")
                return
            if action == "play_pause":
                if vc is None:
                    await self._reply(interaction, "The voice connection was lost.")
                    return
                if vc.is_paused():
                    vc.resume()
                    message = "Resumed."
                else:
                    vc.pause()
                    message = "Paused."
                await self._reply(interaction, message)
                return
            if action == "repeat":
                session.repeat_current = not session.repeat_current
                if session.panel_message is not None:
                    await self._update_panel(session)
                state = "on" if session.repeat_current else "off"
                await self._reply(interaction, f"Repeat is {state}.")
                return
            if action == "previous":
                if not session.history:
                    await self._reply(interaction, "There is no previous song yet.")
                    return
                if vc is None:
                    await self._reply(interaction, "The voice connection was lost.")
                    return
                interrupted = session.current
                previous = session.history.pop()
                if interrupted is not None:
                    session.queue.appendleft(interrupted)
                session.queue.appendleft(previous)
                session.current = None
                session.playback_generation += 1
                vc.stop()
                await self._start_next(session)
                await self._reply(interaction, "Playing the previous song.")
                return
            await self._reply(interaction, "Unknown music control.")

    async def on_voice_disconnected(self, guild_id: int) -> None:
        """Discard playback state after any manual or automatic voice leave."""
        guild_id = int(guild_id)
        # Retain the lock object so waiters and future operations cannot split
        # into two independent critical sections for the same guild.
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            session = self.sessions.pop(guild_id, None)
            if session is None:
                return
            session.playback_generation += 1
            session.queue.clear()
            session.history.clear()
            session.repeat_current = False
            session.current = None
            if session.panel_message is not None:
                await self._update_panel(session)

    async def admin_action(self, interaction, guild_id: int, action: str) -> None:
        administrator = bool(
            getattr(
                getattr(interaction.user, "guild_permissions", None),
                "administrator",
                False,
            )
        )
        if not administrator:
            await self._reply(
                interaction,
                "This command requires the Discord Administrator permission.",
            )
            return
        if hasattr(interaction.response, "defer"):
            await interaction.response.defer(ephemeral=True)
        guild_id = int(guild_id)
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            session = self.sessions.get(guild_id)
            if session is None:
                await self._reply(interaction, "There is no active music queue.")
                return
            vc = self.adapter._voice_clients.get(guild_id)
            if action == "clear":
                session.queue.clear()
                session.history.clear()
                session.repeat_current = False
                if vc is not None and session.current is not None:
                    session.playback_generation += 1
                session.current = None
                if vc is not None:
                    vc.stop()
                if session.panel_message is not None:
                    await self._update_panel(session)
                await self._restore_voice_listening(guild_id)
                await self._reply(interaction, "Music queue cleared.")
                return
            if action == "forceskip":
                if vc is None:
                    await self._reply(interaction, "The voice connection was lost.")
                    return
                vc.stop()
                await self._reply(interaction, "Force-skipped.")
                return
            await self._reply(interaction, "Unknown administrator action.")

    async def _on_track_end(
        self,
        guild_id: int,
        error=None,
        *,
        generation: Optional[int] = None,
    ) -> None:
        if error:
            logger.warning(
                "Discord music playback failed in guild %s: %s", guild_id, error
            )
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            session = self.sessions.get(guild_id)
            if session is None:
                return
            if generation is not None and generation != session.playback_generation:
                return
            if session.current is None:
                return
            finished = session.current
            session.current = None
            if session.repeat_current and error is None:
                session.queue.appendleft(finished)
            else:
                session.history.append(finished)
            await self._start_next(session)
            await self._update_panel(session)

    async def _restore_voice_listening(self, guild_id: int) -> None:
        receiver = getattr(self.adapter, "_voice_receivers", {}).get(guild_id)
        if receiver is not None:
            try:
                receiver.resume()
            except Exception:
                logger.debug("Failed to resume Discord voice receiver", exc_info=True)
        voice_fx = getattr(self.adapter, "_voice_fx_cfg", {}) or {}
        mixers = getattr(self.adapter, "_voice_mixers", {})
        vc = getattr(self.adapter, "_voice_clients", {}).get(guild_id)
        install_mixer = getattr(self.adapter, "_install_voice_mixer", None)
        if (
            voice_fx.get("enabled")
            and guild_id not in mixers
            and vc is not None
            and callable(install_mixer)
        ):
            try:
                await install_mixer(guild_id, vc)
            except Exception:
                logger.warning("Failed to restore Discord voice mixer", exc_info=True)
        reset_timeout = getattr(self.adapter, "_reset_voice_timeout", None)
        if callable(reset_timeout):
            reset_timeout(guild_id)


try:
    import discord
except ImportError:  # pragma: no cover - Discord adapter is unavailable too
    discord = None


if discord is not None:

    class MusicControlView(discord.ui.View):
        """Persistent public controls; authorization is checked per click."""

        _BUTTONS = (
            ("Previous", "hermes_music_previous", "secondary", "previous"),
            ("Play/Pause", "hermes_music_play_pause", "primary", "play_pause"),
            ("Repeat", "hermes_music_repeat", "secondary", "repeat"),
            ("Skip", "hermes_music_skip", "danger", "skip"),
        )

        def __init__(self, manager: DiscordMusicManager, guild_id: int):
            super().__init__(timeout=None)
            self.manager = manager
            self.guild_id = int(guild_id)
            for label, custom_id, style_name, action in self._BUTTONS:
                button = discord.ui.Button(
                    label=label,
                    style=getattr(discord.ButtonStyle, style_name),
                    custom_id=custom_id,
                )

                async def callback(interaction, _action=action):
                    await self.manager.control(interaction, self.guild_id, _action)

                button.callback = callback
                self.add_item(button)

else:

    class MusicControlView:  # pragma: no cover - import guard only
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("discord.py is required for music controls")
