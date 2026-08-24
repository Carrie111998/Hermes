"""Discord music queue and playback-control behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.discord.music import (
    DiscordMusicManager,
    MusicControlView,
    MusicResolutionError,
    MusicSession,
    MusicTrack,
    TrackResolver,
)
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _track(title: str, requester_id: int = 42) -> MusicTrack:
    return MusicTrack(
        title=title,
        webpage_url=f"https://example.com/{title}",
        lookup=f"ytsearch1:{title}",
        requester_id=requester_id,
        requester_name=f"user-{requester_id}",
    )


def test_music_session_exposes_now_playing_and_fifo_queue():
    session = MusicSession(guild_id=7)
    first = _track("first")
    second = _track("second", requester_id=99)

    session.enqueue([first, second])
    session.current = session.queue.popleft()

    assert session.current is first
    assert list(session.queue) == [second]
    assert "first" in session.render_queue()
    assert "second" in session.render_queue()
    assert "user-99" in session.render_queue()


def test_only_current_requester_or_administrator_can_use_playback_controls():
    session = MusicSession(guild_id=7, current=_track("current", requester_id=42))

    assert session.can_control(user_id=42, administrator=False)
    assert not session.can_control(user_id=99, administrator=False)
    assert session.can_control(user_id=99, administrator=True)


def test_spotify_track_is_translated_to_a_searchable_audio_track():
    resolver = TrackResolver(
        spotify_metadata=lambda _url: [
            {
                "title": "Cut To The Feeling",
                "artist": "Carly Rae Jepsen",
                "webpage_url": "https://open.spotify.com/track/abc",
                "thumbnail": "https://i.scdn.co/image/cover",
            }
        ]
    )

    tracks = resolver.resolve(
        "https://open.spotify.com/track/abc",
        requester_id=42,
        requester_name="nahv",
    )

    assert len(tracks) == 1
    assert tracks[0].title == "Cut To The Feeling — Carly Rae Jepsen"
    assert tracks[0].lookup == "ytsearch5:Cut To The Feeling Carly Rae Jepsen audio"
    assert tracks[0].requester_id == 42


def test_spotify_public_metadata_fallback_extracts_artist_title_and_cover():
    class Response:
        def __init__(
            self, *, text="", payload=None, url="https://open.spotify.com/track/abc"
        ):
            self.text = text
            self._payload = payload
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def http_get(url, **kwargs):
        if "/oembed" in url:
            return Response(
                payload={"title": "A Song", "thumbnail_url": "https://i.scdn.co/cover"}
            )
        return Response(
            text='<meta property="og:description" content="Artist Name · A Song · Song · 2026">'
        )

    resolver = TrackResolver(http_get=http_get)
    tracks = resolver.resolve(
        "https://open.spotify.com/track/abc",
        requester_id=42,
        requester_name="nahv",
    )

    assert tracks[0].title == "A Song — Artist Name"
    assert tracks[0].thumbnail == "https://i.scdn.co/cover"


def test_spotify_album_and_playlist_links_are_rejected_explicitly():
    resolver = TrackResolver(http_get=MagicMock(), url_guard=lambda _url: True)

    with pytest.raises(MusicResolutionError, match="Spotify track links"):
        resolver.resolve(
            "https://open.spotify.com/playlist/abc",
            requester_id=42,
            requester_name="nahv",
        )


def test_spotify_short_link_cannot_redirect_to_a_collection():
    class Response:
        text = ""
        url = "https://open.spotify.com/playlist/abc"

        def raise_for_status(self):
            return None

        def json(self):
            return {"title": "Playlist", "thumbnail_url": None}

    resolver = TrackResolver(
        http_get=lambda *_args, **_kwargs: Response(), url_guard=lambda _url: True
    )
    with pytest.raises(MusicResolutionError, match="Spotify track links"):
        resolver.resolve(
            "https://spotify.link/short",
            requester_id=42,
            requester_name="nahv",
        )


def test_youtube_link_metadata_and_fresh_audio_stream_are_resolved_lazily():
    calls = []

    def extract(query, *, flat):
        calls.append((query, flat))
        if flat:
            return {
                "title": "A Song",
                "webpage_url": "https://www.youtube.com/watch?v=abc",
                "duration": 123,
                "thumbnail": "https://i.ytimg.com/abc.jpg",
            }
        return {
            "title": "A Song",
            "webpage_url": "https://www.youtube.com/watch?v=abc",
            "url": "https://rr.example.googlevideo.com/audio",
        }

    resolver = TrackResolver(media_extractor=extract, url_guard=lambda _url: True)
    tracks = resolver.resolve(
        "https://www.youtube.com/watch?v=abc",
        requester_id=42,
        requester_name="nahv",
    )

    assert tracks[0].title == "A Song"
    assert calls == [("https://www.youtube.com/watch?v=abc", True)]
    assert (
        resolver.resolve_stream(tracks[0]) == "https://rr.example.googlevideo.com/audio"
    )
    assert calls[-1] == ("https://www.youtube.com/watch?v=abc", False)


def test_text_search_queues_one_result_but_keeps_playback_fallback_candidates():
    def extract(query, *, flat):
        assert query == "ytsearch5:requested song"
        assert flat is True
        return {
            "entries": [
                {
                    "title": "First result",
                    "webpage_url": "https://www.youtube.com/watch?v=first",
                },
                {
                    "title": "Second result",
                    "webpage_url": "https://www.youtube.com/watch?v=second",
                },
            ]
        }

    tracks = TrackResolver(media_extractor=extract).resolve(
        "requested song",
        requester_id=42,
        requester_name="nahv",
    )

    assert [track.title for track in tracks] == ["First result"]
    assert tracks[0].lookup == "ytsearch5:requested song"


def test_ytdlp_prefers_low_bandwidth_hls_and_enables_packaged_js_runtime(monkeypatch):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_info(self, query, download):
            assert query == "https://www.youtube.com/watch?v=abc"
            assert download is False
            return {"url": "https://manifest.googlevideo.com/audio.m3u8"}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYoutubeDL)

    TrackResolver._extract_media(
        "https://www.youtube.com/watch?v=abc",
        flat=False,
    )

    assert captured["format"] == "worst[protocol^=m3u8][acodec!=none]"
    assert captured["ignoreerrors"] is True
    assert "node" in captured["js_runtimes"]


def test_non_http_or_untrusted_extractor_streams_are_rejected():
    track = _track("unsafe")
    file_resolver = TrackResolver(
        media_extractor=lambda *_args, **_kwargs: {"url": "file:///etc/passwd"},
        url_guard=lambda _url: True,
    )
    with pytest.raises(MusicResolutionError, match="HTTP or HTTPS"):
        file_resolver.resolve_stream(track)

    untrusted_resolver = TrackResolver(
        media_extractor=lambda *_args, **_kwargs: {
            "url": "https://attacker.example/audio"
        },
        url_guard=lambda _url: True,
    )
    with pytest.raises(MusicResolutionError, match="trusted media host"):
        untrusted_resolver.resolve_stream(track)


def test_youtube_hls_is_limited_to_provider_controlled_manifest_host():
    track = MusicTrack(
        title="youtube",
        webpage_url="https://www.youtube.com/watch?v=abc",
        lookup="https://www.youtube.com/watch?v=abc",
        requester_id=42,
        requester_name="nahv",
    )
    resolver = TrackResolver(
        media_extractor=lambda *_args, **_kwargs: {
            "url": "https://attacker.akamaized.net/playlist.m3u8",
            "protocol": "m3u8_native",
        },
        url_guard=lambda _url: True,
    )

    with pytest.raises(MusicResolutionError, match="trusted manifest host"):
        resolver.resolve_stream(track)


def test_ffmpeg_source_restricts_nested_network_protocols(monkeypatch):
    ffmpeg = MagicMock(return_value="source")
    monkeypatch.setattr("discord.FFmpegPCMAudio", ffmpeg)

    source = DiscordMusicManager._default_audio_source(
        "https://manifest.googlevideo.com/audio.m3u8"
    )

    assert source == "source"
    before_options = ffmpeg.call_args.kwargs["before_options"]
    assert ffmpeg.call_args.kwargs["executable"]
    assert "-protocol_whitelist http,https,tcp,tls,crypto" in before_options
    assert "file" not in before_options


def test_spotify_http_uses_connect_time_ssrf_safe_client(monkeypatch):
    client = MagicMock()
    client.get.return_value = "response"
    context = MagicMock()
    context.__enter__.return_value = client
    monkeypatch.setattr(
        "tools.url_safety.create_ssrf_safe_client",
        MagicMock(return_value=context),
    )

    result = TrackResolver._default_http_get(
        "https://open.spotify.com/track/abc",
        follow_redirects=True,
        timeout=15.0,
    )

    assert result == "response"
    client.get.assert_called_once_with("https://open.spotify.com/track/abc")


def test_private_or_local_media_urls_are_rejected_before_extraction():
    extractor = MagicMock()
    resolver = TrackResolver(
        media_extractor=extractor,
        url_guard=lambda url: not url.startswith("http://127.0.0.1"),
    )

    with pytest.raises(MusicResolutionError, match="private or local"):
        resolver.resolve(
            "http://127.0.0.1:8080/secrets",
            requester_id=42,
            requester_name="nahv",
        )

    extractor.assert_not_called()


def test_generic_web_urls_without_a_supported_extractor_are_rejected():
    extractor = MagicMock()
    resolver = TrackResolver(
        media_extractor=extractor,
        url_guard=lambda _url: True,
        url_support_checker=lambda _url: False,
    )

    with pytest.raises(MusicResolutionError, match="supported streaming platform"):
        resolver.resolve(
            "https://attacker.example/redirect",
            requester_id=42,
            requester_name="nahv",
        )

    extractor.assert_not_called()


def test_only_curated_streaming_hosts_are_allowed_for_ytdlp():
    assert TrackResolver._has_supported_extractor("https://www.youtube.com/watch?v=abc")
    assert not TrackResolver._has_supported_extractor(
        "https://www.facebook.com/watch/abc"
    )


@pytest.mark.asyncio
async def test_panel_disables_mentions_with_compatible_allowed_mentions(monkeypatch):
    import discord

    class CompatibleAllowedMentions:
        def __init__(self, *, everyone=True, roles=True, users=True, replied_user=True):
            self.everyone = everyone
            self.roles = roles
            self.users = users
            self.replied_user = replied_user

    monkeypatch.setattr(discord, "AllowedMentions", CompatibleAllowedMentions)
    manager = DiscordMusicManager(SimpleNamespace(_voice_clients={}))
    session = MusicSession(guild_id=7, current=_track("@everyone"))
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()

    await manager._update_panel(session)

    mentions = session.panel_message.edit.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


@pytest.mark.asyncio
async def test_add_joins_requesters_vc_starts_fifo_playback_and_updates_public_panel():
    track = _track("first")
    resolver = MagicMock()
    resolver.resolve.return_value = [track]
    resolver.resolve_stream.return_value = "https://cdn.example/audio"

    panel = SimpleNamespace(edit=AsyncMock())
    text_channel = SimpleNamespace(send=AsyncMock(return_value=panel))
    voice_channel = SimpleNamespace(id=12, guild=SimpleNamespace(id=7))
    voice_client = MagicMock()
    voice_client.is_connected.return_value = True
    voice_client.is_playing.return_value = False
    adapter = SimpleNamespace(
        _voice_clients={7: voice_client},
        _voice_receivers={},
        _voice_mixers={},
        join_voice_channel=AsyncMock(return_value=True),
        _cancel_voice_timeout=MagicMock(),
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=7),
        channel=text_channel,
        user=SimpleNamespace(
            id=42,
            display_name="nahv",
            voice=SimpleNamespace(channel=voice_channel),
        ),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    source = object()
    manager = DiscordMusicManager(
        adapter,
        resolver=resolver,
        audio_source_factory=lambda _url: source,
        view_factory=lambda _manager, _guild_id: "controls",
    )

    await manager.add(interaction, "first")

    adapter.join_voice_channel.assert_awaited_once_with(
        voice_channel,
        text_channel_id=None,
    )
    voice_client.play.assert_called_once()
    assert voice_client.play.call_args.args[0] is source
    assert manager.sessions[7].current is track
    text_channel.send.assert_awaited_once()
    panel.edit.assert_awaited()
    assert "first" in panel.edit.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_unplayable_track_is_skipped_and_next_track_starts():
    bad = _track("bad")
    good = _track("good")
    resolver = MagicMock()
    resolver.resolve_stream.side_effect = [
        MusicResolutionError("unplayable"),
        "https://cdn.example/good",
    ]
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    adapter = SimpleNamespace(
        _voice_clients={7: vc},
        _voice_receivers={},
        _voice_mixers={},
        _cancel_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(
        adapter, resolver=resolver, view_factory=lambda *_args: "controls"
    )
    session = MusicSession(guild_id=7)
    session.enqueue([bad, good])
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()

    await manager._start_next(session)

    assert session.current is good
    vc.play.assert_called_once()


@pytest.mark.asyncio
async def test_skip_button_rejects_non_requester_and_allows_current_requester():
    vc = MagicMock()
    adapter = SimpleNamespace(_voice_clients={7: vc})
    manager = DiscordMusicManager(
        adapter,
        view_factory=lambda *_args: "controls",
    )
    manager.sessions[7] = MusicSession(guild_id=7, current=_track("current", 42))

    denied = SimpleNamespace(
        user=SimpleNamespace(
            id=99, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    allowed = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.control(denied, 7, "skip")
    vc.stop.assert_not_called()
    assert denied.response.send_message.await_args.kwargs["ephemeral"] is True

    await manager.control(allowed, 7, "skip")
    vc.stop.assert_called_once_with()


@pytest.mark.asyncio
async def test_play_pause_button_toggles_voice_client_state():
    vc = MagicMock()
    vc.is_paused.side_effect = [False, True]
    adapter = SimpleNamespace(_voice_clients={7: vc})
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    manager.sessions[7] = MusicSession(guild_id=7, current=_track("current", 42))
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.control(interaction, 7, "play_pause")
    vc.pause.assert_called_once_with()

    await manager.control(interaction, 7, "play_pause")
    vc.resume.assert_called_once_with()


@pytest.mark.asyncio
async def test_repeat_button_toggles_current_track_repeat_mode():
    adapter = SimpleNamespace(_voice_clients={7: MagicMock()})
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    session = MusicSession(guild_id=7, current=_track("current", 42))
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.control(interaction, 7, "repeat")
    assert session.repeat_current is True

    await manager.control(interaction, 7, "repeat")
    assert session.repeat_current is False


@pytest.mark.asyncio
async def test_previous_button_replays_history_then_returns_to_interrupted_song():
    previous = _track("previous", 42)
    current = _track("current", 42)
    resolver = MagicMock()
    resolver.resolve_stream.return_value = "https://cdn.example/audio"
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    vc.is_paused.return_value = False
    adapter = SimpleNamespace(
        _voice_clients={7: vc},
        _voice_receivers={},
        _voice_mixers={},
        _cancel_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(
        adapter,
        resolver=resolver,
        audio_source_factory=lambda _url: object(),
        view_factory=lambda *_args: "controls",
    )
    session = MusicSession(guild_id=7, current=current)
    session.history.append(previous)
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.control(interaction, 7, "previous")

    assert session.current is previous
    assert list(session.queue)[0] is current
    vc.stop.assert_called_once_with()
    vc.play.assert_called_once()


@pytest.mark.asyncio
async def test_previous_defers_before_resolving_stream():
    previous = _track("previous", 42)
    current = _track("current", 42)
    resolver = MagicMock()
    resolver.resolve_stream.return_value = "https://cdn.example/previous"
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    adapter = SimpleNamespace(
        _voice_clients={7: vc},
        _voice_receivers={},
        _voice_mixers={},
        _cancel_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(
        adapter, resolver=resolver, view_factory=lambda *_args: "controls"
    )
    session = MusicSession(guild_id=7, current=current)
    session.history.append(previous)
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await manager.control(interaction, 7, "previous")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_voice_client_does_not_claim_skip_succeeded():
    adapter = SimpleNamespace(_voice_clients={})
    manager = DiscordMusicManager(adapter)
    session = MusicSession(guild_id=7, current=_track("current", 42))
    manager.sessions[7] = session
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.control(interaction, 7, "skip")

    assert session.current is not None
    assert "lost" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_music_panel_has_previous_play_pause_repeat_and_skip_buttons():
    manager = SimpleNamespace(control=AsyncMock())
    view = MusicControlView(manager, 7)

    labels = {item.label for item in view.children}
    assert labels == {"Previous", "Play/Pause", "Repeat", "Skip"}
    assert view.timeout is None


@pytest.mark.asyncio
async def test_finishing_an_empty_queue_restores_voice_listening_and_inactivity_timer():
    receiver = SimpleNamespace(resume=MagicMock())
    adapter = SimpleNamespace(
        _voice_clients={7: MagicMock()},
        _voice_receivers={7: receiver},
        _voice_mixers={},
        _voice_fx_cfg={"enabled": True},
        _install_voice_mixer=AsyncMock(),
        _reset_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    session = MusicSession(guild_id=7, current=_track("last", 42))
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session

    await manager._on_track_end(7)

    assert session.current is None
    receiver.resume.assert_called_once_with()
    adapter._install_voice_mixer.assert_awaited_once_with(7, adapter._voice_clients[7])
    adapter._reset_voice_timeout.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_voice_disconnect_discards_stale_music_state_and_updates_panel():
    adapter = SimpleNamespace(_voice_clients={})
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    session = MusicSession(guild_id=7, current=_track("current", 42))
    session.enqueue([_track("next", 99)])
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session

    await manager.on_voice_disconnected(7)

    assert 7 not in manager.sessions
    session.panel_message.edit.assert_awaited_once()
    assert (
        "Nothing is playing" in session.panel_message.edit.await_args.kwargs["content"]
    )


@pytest.mark.asyncio
async def test_voice_disconnect_serializes_on_persistent_guild_lock():
    import asyncio

    adapter = SimpleNamespace(_voice_clients={})
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    session = MusicSession(guild_id=7, current=_track("current", 42))
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session
    lock = manager._locks.setdefault(7, asyncio.Lock())

    await lock.acquire()
    disconnect = asyncio.create_task(manager.on_voice_disconnected(7))
    await asyncio.sleep(0)

    assert 7 in manager.sessions

    lock.release()
    await disconnect

    assert 7 not in manager.sessions
    assert manager._locks[7] is lock


@pytest.mark.asyncio
async def test_show_queue_defers_before_waiting_for_guild_lock():
    import asyncio

    adapter = SimpleNamespace(_voice_clients={})
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    session = MusicSession(guild_id=7, current=_track("current", 42))
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    manager.sessions[7] = session
    interaction = SimpleNamespace(
        channel=SimpleNamespace(),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    lock = manager._locks.setdefault(7, asyncio.Lock())

    await lock.acquire()
    show = asyncio.create_task(manager.show_queue(interaction, 7))
    await asyncio.sleep(0)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    session.panel_message.edit.assert_not_awaited()

    lock.release()
    await show

    session.panel_message.edit.assert_awaited_once()
    interaction.followup.send.assert_awaited_once_with(
        "The public music queue is up to date.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_adapter_voice_leave_notifies_music_manager():
    adapter = object.__new__(DiscordAdapter)
    adapter._voice_locks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._client = None
    adapter._voice_mixers = {}
    adapter._voice_clients = {7: SimpleNamespace(is_connected=lambda: False)}
    adapter._voice_timeout_tasks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._music_manager = SimpleNamespace(on_voice_disconnected=AsyncMock())

    await adapter.leave_voice_channel(7)

    adapter._music_manager.on_voice_disconnected.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_clear_queue_is_administrator_only_and_stops_current_song():
    vc = MagicMock()
    receiver = SimpleNamespace(resume=MagicMock())
    adapter = SimpleNamespace(
        _voice_clients={7: vc},
        _voice_receivers={7: receiver},
        _reset_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(adapter, view_factory=lambda *_args: "controls")
    session = MusicSession(guild_id=7, current=_track("current", 42))
    session.enqueue([_track("next", 99)])
    manager.sessions[7] = session

    member = SimpleNamespace(
        user=SimpleNamespace(
            id=42, guild_permissions=SimpleNamespace(administrator=False)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    admin = SimpleNamespace(
        user=SimpleNamespace(
            id=1, guild_permissions=SimpleNamespace(administrator=True)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.admin_action(member, 7, "clear")
    assert list(session.queue)
    vc.stop.assert_not_called()

    await manager.admin_action(admin, 7, "clear")
    assert not session.queue
    assert session.current is None
    vc.stop.assert_called_once_with()
    receiver.resume.assert_called_once_with()
    adapter._reset_voice_timeout.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_clear_suppresses_delayed_stop_callback_from_old_track():
    vc = MagicMock()
    adapter = SimpleNamespace(
        _voice_clients={7: vc},
        _voice_receivers={},
        _reset_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(adapter)
    session = MusicSession(guild_id=7, current=_track("old", 42))
    old_generation = session.playback_generation
    manager.sessions[7] = session
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=1, guild_permissions=SimpleNamespace(administrator=True)
        ),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await manager.admin_action(interaction, 7, "clear")
    replacement = _track("replacement", 99)
    session.current = replacement
    await manager._on_track_end(7, generation=old_generation)

    assert session.current is replacement


@pytest.mark.asyncio
async def test_stale_callback_cannot_consume_replacement_track_if_callbacks_reorder():
    adapter = SimpleNamespace(
        _voice_clients={},
        _voice_receivers={},
        _voice_mixers={},
        _reset_voice_timeout=MagicMock(),
    )
    manager = DiscordMusicManager(adapter)
    replacement = _track("replacement", 99)
    session = MusicSession(guild_id=7, current=replacement)
    session.playback_generation = 2
    session.panel_message = SimpleNamespace(edit=AsyncMock())
    session.text_channel = SimpleNamespace()
    manager.sessions[7] = session

    await manager._on_track_end(7, generation=1)

    assert session.current is replacement
    assert not session.history

    await manager._on_track_end(7, generation=2)

    assert session.current is None
    assert list(session.history) == [replacement]


@pytest.mark.asyncio
async def test_admin_action_defers_before_waiting_for_guild_lock():
    vc = MagicMock()
    adapter = SimpleNamespace(_voice_clients={7: vc})
    manager = DiscordMusicManager(adapter)
    manager.sessions[7] = MusicSession(guild_id=7, current=_track("current", 42))
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=1, guild_permissions=SimpleNamespace(administrator=True)
        ),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await manager.admin_action(interaction, 7, "forceskip")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()


class _FakeTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def decorator(callback):
            self.commands[name] = callback
            return callback

        return decorator

    def add_command(self, command):
        self.commands[command.name] = command

    def get_commands(self):
        return [SimpleNamespace(name=name) for name in self.commands]


def test_discord_registers_music_queue_and_admin_slash_commands():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(tree=_FakeTree())

    adapter._register_slash_commands()

    assert {"play", "musicqueue", "forceskip", "clearqueue"}.issubset(
        adapter._client.tree.commands
    )
