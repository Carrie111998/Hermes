"""Room profile persistence and resolver coverage."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import multiprocessing
import pytest

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.room_profiles import RoomModelProfile, RoomProfileStore, exact_room_id
from gateway.run import GatewayRunner, _get_channel_override
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_cli.model_switch import parse_model_switch_args

P = RoomModelProfile("m", "p", "high")
def src(platform=Platform.NEXTDO, chat_id="chat", thread_id=None):
    return SessionSource(platform=platform, chat_id=chat_id, user_id="u", thread_id=thread_id)
def runner(config=None):
    r=object.__new__(GatewayRunner); r.config=config or GatewayConfig(); r._sessions={}; r._pending_model_notes={}; r._agent_cache={}; r._room_profile_stores={}; r._room_profile_stores_lock=__import__('threading').Lock(); r._session_key_for_source=lambda s:f'{s.platform.value}:{s.chat_id}'; r._normalize_source_for_session_key=lambda s:s; r._evict_cached_agent=lambda k:r._agent_cache.pop(k,None); r._async_session_store=SimpleNamespace(set_model_override=AsyncMock()); return r

def test_missing_create_reload_and_exact_identity(tmp_path):
    path=tmp_path/'room_profiles.json'; s=RoomProfileStore(path)
    assert s.get('nextdo','r') is None
    s.upsert('nextdo','r',P)
    assert json.loads(path.read_text())['version']==1
    assert RoomProfileStore(path).get('nextdo','r') == P
    assert exact_room_id(src(thread_id=' t '))=='t' and exact_room_id(src())=='chat'

def test_siblings_platforms_threads_and_unknown_fields_preserved(tmp_path):
    path=tmp_path/'room_profiles.json'; s=RoomProfileStore(path)
    s.upsert('nextdo','a',P); s.upsert('nextdo','b',RoomModelProfile('b','p','low')); s.upsert('discord','a',P)
    raw=json.loads(path.read_text()); raw['future']=42; raw['profiles']['nextdo']['a']['future_entry']='x'; path.write_text(json.dumps(raw))
    s.upsert('nextdo','c',P); raw=json.loads(path.read_text())
    assert set(raw['profiles']['nextdo'])=={'a','b','c'} and set(raw['profiles']['discord'])=={'a'} and raw['future']==42

def test_thread_concurrency_preserves_every_room(tmp_path):
    path=tmp_path/'room_profiles.json'; s=RoomProfileStore(path)
    with ThreadPoolExecutor(8) as pool: list(pool.map(lambda i:s.upsert('nextdo',str(i),P), range(20)))
    assert len(json.loads(path.read_text())['profiles']['nextdo'])==20

def _mp_upsert(args):
    path, room=args; RoomProfileStore(path).upsert('nextdo', room, P)

def test_multiprocess_lock_preserves_every_room(tmp_path):
    path=str(tmp_path/'room_profiles.json')
    with multiprocessing.get_context('fork').Pool(4) as pool: pool.map(_mp_upsert, [(path,str(i)) for i in range(8)])
    assert len(json.loads(open(path).read())['profiles']['nextdo'])==8

def test_corrupt_unsupported_and_invalid_entry_fail_closed(tmp_path, caplog):
    path=tmp_path/'room_profiles.json'; path.write_text('{bad')
    s=RoomProfileStore(path); assert s.get('nextdo','x') is None
    with pytest.raises(ValueError): s.upsert('nextdo','x',P)
    path.write_text(json.dumps({'version':2,'profiles':{}})); s.load(); assert s.get('nextdo','x') is None
    path.write_text(json.dumps({'version':1,'profiles':{'nextdo':{'bad':{},'good':P.__dict__}}})); s.load(); assert s.get('nextdo','good')==P

def test_atomic_failure_preserves_bytes_and_cache(tmp_path, monkeypatch):
    path=tmp_path/'room_profiles.json'; s=RoomProfileStore(path); s.upsert('nextdo','old',P); before=path.read_bytes(); old=s.get('nextdo','old')
    monkeypatch.setattr('gateway.room_profiles.atomic_json_write', lambda *a,**k: (_ for _ in ()).throw(OSError('full')))
    with pytest.raises(OSError): s.upsert('nextdo','new',P)
    assert path.read_bytes()==before and s.get('nextdo','old')==old and s.get('nextdo','new') is None

@pytest.mark.asyncio
async def test_trusted_command_persists_sidecar_and_clears_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", lambda **kw: SimpleNamespace(success=True, new_model=kw["raw_input"], target_provider=kw["explicit_provider"]))
    r = runner()
    source = src()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    key = store.get_or_create_session(source).session_key
    r._session_key_for_source = lambda s: key
    r.session_store = store
    r._async_session_store = AsyncSessionStore(store)
    store.set_model_override(key, {"model": "old"})
    state = r._session_state(key)
    state.conversation.model_override = {"model": "old"}; state.conversation.reasoning_override = {"enabled": True, "effort": "low"}; r._agent_cache[key] = object()
    event = MessageEvent(text="/model", source=source, metadata={"route_kind":"internal_room_model_preset", "synthetic":True, "preset_key":"sol-max"})
    result = await r._handle_room_model_request(event, parse_model_switch_args("gpt-5.6-sol --provider openai-codex --reasoning max --room"))
    assert result.startswith("✅")
    profile_path = tmp_path / "room_profiles.json"
    assert profile_path.is_file()
    assert RoomProfileStore(profile_path).get("nextdo", "chat") == RoomModelProfile("gpt-5.6-sol", "openai-codex", "max")
    assert SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig()).get_model_override(key) is None
    assert state.conversation.model_override is None and state.conversation.reasoning_override is None and key not in r._agent_cache

def test_full_precedence_and_static_prompt_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path)); r=runner(GatewayConfig(platforms={Platform.NEXTDO:PlatformConfig(channel_overrides={'chat':ChannelOverride(model='static',reasoning='medium',system_prompt='prompt')})}))
    RoomProfileStore(tmp_path/'room_profiles.json').upsert('nextdo','chat',RoomModelProfile('dynamic','p','high'))
    assert r._resolve_model_for_channel(Platform.NEXTDO,'chat')=='dynamic'
    assert r._resolve_session_reasoning_config(source=src(),model='global')['effort']=='high'
    assert r._get_system_prompt_for_channel(Platform.NEXTDO,'chat')=='prompt'
    r._session_state('nextdo:chat').conversation.model_override={'model':'session','provider':'p','api_key':'k'}
    assert r._resolve_session_agent_runtime(source=src())[0]=='session'

def test_static_lookup_remains_upstream_chat_first():
    c=GatewayConfig.from_dict({'platforms':{'nextdo':{'channel_overrides':{'chat':{'model':'chat'},'thread':{'model':'thread'}}}}})
    assert _get_channel_override(c,Platform.NEXTDO,'chat',thread_id='thread').model=='chat'
