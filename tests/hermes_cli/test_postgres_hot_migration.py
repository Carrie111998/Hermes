"""Security/provenance behavior tests for the opt-in importer."""
from __future__ import annotations
import asyncio, contextlib, errno, hashlib, os, shutil, signal, socket, sqlite3, subprocess, sys, time, types
from pathlib import Path
import pytest
import hermes_cli.postgres_hot_migration as migration
from hermes_cli.postgres_hot_migration import EXPECTED_DOMAIN_CHECK_DEFINITION, MigrationError, TABLE_COLUMNS, _domain_check_matches, _expected_type, _migrate_approved as migrate, _order_by, _preflight_approved as preflight, _projected_storage, _sha256_file, main, parse_target_dsn

def _fixture_dbs(tmp_path: Path) -> tuple[Path, Path]:
    state, logs = tmp_path / "state.db", tmp_path / "logs.db"; c = sqlite3.connect(state)
    for table, columns in TABLE_COLUMNS.items():
        if table == "agent_logs": continue
        defs=[]
        for col in columns:
            typ="INTEGER" if table=="messages" and col=="id" else _expected_type(col)
            defs.append(f'"{col}" {typ}')
        if table=="session_model_usage": defs.append("PRIMARY KEY(session_id,model,billing_provider,billing_base_url,billing_mode,task)")
        elif table=="gateway_routing": defs.append("PRIMARY KEY(scope,session_key)")
        else: defs.append(f"PRIMARY KEY({columns[0]})")
        c.execute(f'CREATE TABLE "{table}" ({", ".join(defs)})')
    s={x:None for x in TABLE_COLUMNS["sessions"]};s.update(id="s1",source="test",started_at=1.0,message_count=1,rewind_count=0,archived=0,compression_fallback_streak=0,compression_ineffective_count=0,pinned=0)
    c.execute(f'INSERT INTO sessions ({",".join(s)}) VALUES ({",".join("?" for _ in s)})',tuple(s.values()))
    m={x:None for x in TABLE_COLUMNS["messages"]};m.update(id=1,session_id="s1",role="user",content="nul\0日本語{bad",timestamp=2.0,active=1,compacted=0)
    c.execute(f'INSERT INTO messages ({",".join(m)}) VALUES ({",".join("?" for _ in m)})',tuple(m.values()));c.commit();c.close()
    l=sqlite3.connect(logs);l.execute("CREATE TABLE agent_logs (id TEXT PRIMARY KEY,agent_name TEXT,task_description TEXT,model_used TEXT,status TEXT,created_at TEXT,trace_id TEXT,span_id TEXT)");l.execute("INSERT INTO agent_logs VALUES (?,?,?,?,?,?,?,?)",('l','a','nul\0x',None,'ok','now',None,None));l.commit();l.close()
    return state,logs

def _hashes(state:Path,logs:Path)->dict[str,migration.SnapshotApproval]:
    state_conn=sqlite3.connect(state); logs_conn=sqlite3.connect(logs)
    try:
        approval = migration.SnapshotApproval(
            _sha256_file(state), _sha256_file(logs),
            migration._fingerprint(migration._sqlite_schema_contract(state_conn, tuple(x for x in TABLE_COLUMNS if x != "agent_logs"))),
            migration._fingerprint(migration._sqlite_schema_contract(logs_conn, ("agent_logs",))),
        )
    finally:
        state_conn.close(); logs_conn.close()
    return {"approval": approval}
def _scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"; scratch.mkdir(exist_ok=True); scratch.chmod(0o700); return scratch
def test_fixture_approval_hashes_and_logs_are_independent(tmp_path:Path)->None:
    s,l=_fixture_dbs(tmp_path)
    approval = _hashes(s,l)
    hashes, _ = preflight(s,l,1,scratch_dir=_scratch(tmp_path),**approval)
    assert hashes == (_sha256_file(s), _sha256_file(l))
    l.write_bytes(l.read_bytes()+b"x")
    with pytest.raises(MigrationError,match="agent logs SHA256"): preflight(s,l,1,scratch_dir=_scratch(tmp_path),**approval)
@pytest.mark.parametrize("poison",[
    "DROP TABLE state_meta; CREATE TABLE state_meta(key,value)",
    "ALTER TABLE state_meta ADD COLUMN unexpected TEXT",
    "ALTER TABLE state_meta ADD COLUMN wrong_default TEXT DEFAULT 'x'",
    "CREATE TABLE unexpected_owned_table (id TEXT PRIMARY KEY)",
])
def test_source_schema_fingerprint_poison_reject(tmp_path:Path,poison:str)->None:
    s,l=_fixture_dbs(tmp_path); approval = _hashes(s,l); c=sqlite3.connect(s);c.executescript(poison);c.commit();c.close()
    with pytest.raises(MigrationError,match="state SHA256"): preflight(s,l,1,scratch_dir=_scratch(tmp_path),**approval)
@pytest.mark.parametrize("dsn",[
    "postgresql://u@localhost/db",
    "postgresql://u:p@localhost/db?sslmode=disable",
    "postgresql://u:p@localhost/db?sslmode=verify-full&sslrootcert=x&sslmode=verify-full",
    "postgresql://u:p%2Fname@localhost/db",
    "postgresql://u:p@localhost/db?unknown=value",
])
def test_dsn_rejects_missing_password_and_non_effective_options(dsn:str)->None:
    with pytest.raises(MigrationError):
        parse_target_dsn(dsn)
@pytest.mark.parametrize(("dsn","password"),[
    ("postgresql://user:p%40ss%2Fword@127.0.0.1/db", "p@ss/word"),
    ("postgresql://user:empty@localhost/db", "empty"),
])
def test_loopback_dsn_decodes_password(dsn:str,password:str)->None:
    assert parse_target_dsn(dsn,allow_insecure_loopback=True).password == password
def test_secret_dsn_parser_redacts_and_rejects_ambiguous_values(tmp_path:Path)->None:
    secret="postgresql://u:very-secret@example.com/db?sslmode=verify-full&sslrootcert=/missing"
    with pytest.raises(MigrationError): parse_target_dsn(secret)
    for bad in ["postgresql://u:p@x/db?sslmode=verify-full&sslmode=verify-full&sslrootcert=x","postgresql://u:p@x/db?sslmode=verify-full&sslrootcert=x&application_name=x","postgresql://u:p@x/db#frag"]:
        with pytest.raises(MigrationError): parse_target_dsn(bad)
    cfg=parse_target_dsn("postgresql://u:very-secret@localhost/db",allow_insecure_loopback=True)
    assert "very-secret" not in repr(cfg) and cfg.password == "very-secret"
def test_cli_has_no_secret_argument_or_secret_error(tmp_path:Path,monkeypatch:pytest.MonkeyPatch,capsys:pytest.CaptureFixture[str])->None:
    s,l=_fixture_dbs(tmp_path); secret="postgresql://u:attack-password@localhost/db";monkeypatch.setenv("HERMES_POSTGRES_MIGRATION_DSN",secret)
    with pytest.raises(SystemExit) as exit_status: main(["--help"])
    assert exit_status.value.code == 0
    assert "target-dsn" not in capsys.readouterr().out and "expected-state-sha256" not in capsys.readouterr().out
    assert main(["--source",str(s),"--agent-logs",str(l),"--scratch-dir",str(_scratch(tmp_path)),"--cutoff","1","--allow-insecure-loopback"]) == 2
    out=capsys.readouterr();assert secret not in out.err and "attack-password" not in out.err
def _free_port()->int:
    with socket.socket() as x:x.bind(("127.0.0.1",0));return x.getsockname()[1]
@pytest.fixture
def pg17()->str:
    if not shutil.which("docker"):pytest.skip("docker unavailable")
    port=_free_port();name=f"hot-{os.getpid()}-{port}";subprocess.run(["docker","run","-d","--rm","--name",name,"--tmpfs","/var/lib/postgresql/data:rw,size=128m","-e","POSTGRES_PASSWORD=test","-p",f"127.0.0.1:{port}:5432","postgres:17-alpine"],check=True,capture_output=True)
    try:
        ready_streak = 0
        for _ in range(120):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            init_complete = "PostgreSQL init process complete; ready for start up." in (logs.stdout + logs.stderr)
            sql_ready = init_complete and subprocess.run(
                ["docker", "exec", name, "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-c", "SHOW server_version_num"],
                capture_output=True,
            ).returncode == 0
            ready_streak = ready_streak + 1 if sql_ready else 0
            if ready_streak >= 3:
                break
            time.sleep(.2)
        if ready_streak < 3:
            pytest.fail("PostgreSQL 17 fixture did not reach final SQL readiness")
        yield f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
    finally: subprocess.run(["docker","rm","-f",name],capture_output=True)
@pytest.mark.e2e
def test_e2e_replay_counters_and_target_poison(tmp_path:Path,pg17:str)->None:
    s,l=_fixture_dbs(tmp_path);kw=dict(source=s,agent_logs=l,scratch_dir=_scratch(tmp_path),target_dsn=pg17,cutoff=1,allow_insecure_loopback=True,ceiling_bytes=100_000_000,safety_margin_bytes=1,**_hashes(s,l))
    first=asyncio.run(migrate(**kw));second=asyncio.run(migrate(**kw));assert first["run_id"]==second["run_id"] and first["domains"]["sessions"]["rows"]==1
    async def poison():
        import asyncpg;c=await asyncpg.connect(pg17)
        try:await c.execute("ALTER TABLE hermes_hot.messages ADD COLUMN poison text")
        finally:await c.close()
    asyncio.run(poison())
    with pytest.raises(MigrationError,match="target schema mismatch"):asyncio.run(migrate(**kw))
def test_cost_preflight_is_conservative(tmp_path:Path)->None:
    s,l=_fixture_dbs(tmp_path)
    with pytest.raises(MigrationError,match="projected storage"): asyncio.run(migrate(source=s,agent_logs=l,scratch_dir=_scratch(tmp_path),target_dsn="postgresql://u:test@localhost/d",cutoff=1,allow_insecure_loopback=True,ceiling_bytes=1,safety_margin_bytes=0,**_hashes(s,l)))

def test_insufficient_scratch_cleans_partial_stage(tmp_path:Path,monkeypatch:pytest.MonkeyPatch)->None:
    s,l=_fixture_dbs(tmp_path);scratch=_scratch(tmp_path);approval=_hashes(s,l)["approval"]
    def no_reflink(*_args:object)->None: raise OSError(errno.EOPNOTSUPP,"unsupported")
    monkeypatch.setattr(migration.fcntl,"ioctl",no_reflink)
    real_frsize=os.statvfs(scratch).f_frsize
    monkeypatch.setattr(migration.os,"fstatvfs",lambda _fd:types.SimpleNamespace(f_bavail=0,f_frsize=real_frsize))
    with pytest.raises(MigrationError,match="insufficient scratch space"):
        migration._stage_sources(s,l,scratch,approval.state_sha256,approval.agent_logs_sha256)
    assert list(scratch.iterdir()) == []

def test_copy_fallback_checks_pinned_destination_filesystem(tmp_path:Path,monkeypatch:pytest.MonkeyPatch)->None:
    s,l=_fixture_dbs(tmp_path);scratch=_scratch(tmp_path);approval=_hashes(s,l)["approval"]
    def no_reflink(*_args:object)->None: raise OSError(errno.EOPNOTSUPP,"unsupported")
    def wrong_path_probe(_path:object)->object: raise AssertionError("must not probe /proc path")
    monkeypatch.setattr(migration.fcntl,"ioctl",no_reflink)
    monkeypatch.setattr(migration.shutil,"disk_usage",wrong_path_probe)
    monkeypatch.setattr(migration,"DEFAULT_STAGE_MARGIN_BYTES",0)
    stage,modes=migration._stage_sources(s,l,scratch,approval.state_sha256,approval.agent_logs_sha256)
    assert modes == {"state":"copy","agent_logs":"copy"}
    migration._cleanup_stage(stage)
    assert list(scratch.iterdir()) == []

@pytest.mark.parametrize("component", ["user", "password", "database", "query"])
@pytest.mark.parametrize("bad", ["%ZZ", "%FF"])
def test_dsn_strict_percent_decoding_is_sanitized(component: str, bad: str) -> None:
    values = {"user": "user", "password": "password", "database": "database", "query": ""}
    values[component] = bad
    query = f"?sslmode={values['query']}&sslrootcert=x" if component == "query" else ""
    with pytest.raises(MigrationError) as error:
        parse_target_dsn(f"postgresql://{values['user']}:{values['password']}@localhost/{values['database']}{query}", allow_insecure_loopback=True)
    assert bad not in str(error.value)

def test_malformed_port_is_sanitized() -> None:
    with pytest.raises(MigrationError) as error:
        parse_target_dsn("postgresql://user:credential@localhost:not-a-port/database", allow_insecure_loopback=True)
    assert "not-a-port" not in str(error.value) and "credential" not in repr(error.value)

@pytest.mark.parametrize("port", ["0", "65536"])
def test_explicit_out_of_range_port_is_rejected(port: str) -> None:
    with pytest.raises(MigrationError):
        parse_target_dsn(f"postgresql://user:password@localhost:{port}/database", allow_insecure_loopback=True)

def test_domain_ordering_uses_declared_key_kinds() -> None:
    assert _order_by("messages") == '"id"'
    assert _order_by("messages", postgres=True) == '"id"'
    assert _order_by("session_model_usage") == 'CAST("session_id" AS BLOB), CAST("model" AS BLOB), CAST("billing_provider" AS BLOB), "billing_base_url", CAST("billing_mode" AS BLOB), CAST("task" AS BLOB)'
    assert _order_by("session_model_usage", postgres=True) == 'convert_to("session_id", \'UTF8\'), convert_to("model", \'UTF8\'), convert_to("billing_provider", \'UTF8\'), "billing_base_url", convert_to("billing_mode", \'UTF8\'), convert_to("task", \'UTF8\')'

def test_domain_check_catalog_definition_is_exact() -> None:
    expected = [{"conname": "domain_manifests_domain_check", "definition": EXPECTED_DOMAIN_CHECK_DEFINITION}]
    assert _domain_check_matches(expected)
    assert not _domain_check_matches([{**expected[0], "definition": EXPECTED_DOMAIN_CHECK_DEFINITION.replace("'agent_logs'", "'agent_logs'::text, 'poison'")}])

def test_pending_wal_and_stage_mutation_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, logs = _fixture_dbs(tmp_path); hashes = _hashes(state, logs)
    wal = state.with_name(state.name + "-wal"); wal.write_bytes(b"pending")
    with pytest.raises(MigrationError, match="pending WAL"):
        preflight(state, logs, 1, scratch_dir=_scratch(tmp_path), **hashes)
    wal.unlink()
    monkeypatch.setattr(migration.fcntl, "ioctl", lambda *args: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "unsupported")))
    changed = False
    def mutate() -> None:
        nonlocal changed
        if not changed:
            changed = True
            with state.open("r+b") as source:
                source.seek(128); source.write(b"altered!"); source.flush(); os.fsync(source.fileno())
    with pytest.raises(MigrationError, match="SHA256"):
        preflight(state, logs, 1, scratch_dir=_scratch(tmp_path), _before_stage_chunk=mutate, **hashes)

def test_simulated_lease_break_rejects_staging_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, logs = _fixture_dbs(tmp_path); scratch = _scratch(tmp_path)
    monkeypatch.setattr(migration.fcntl, "ioctl", lambda *args: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "unsupported")))

    @contextlib.contextmanager
    def broken_lease(_fd: int):
        yield lambda: True

    monkeypatch.setattr(migration, "_source_read_lease", broken_lease)
    with pytest.raises(MigrationError, match="changed during staging"):
        preflight(state, logs, 1, scratch_dir=scratch, **_hashes(state, logs))
    assert not list(scratch.iterdir())


@pytest.mark.skipif(not sys.platform.startswith("linux") or not hasattr(migration.fcntl, "F_SETLEASE"), reason="Linux leases unavailable")
def test_linux_read_lease_rejects_separate_process_write_open(tmp_path: Path) -> None:
    source = tmp_path / "leased.sqlite"; source.write_bytes(b"original")
    attempted = tmp_path / "write-open-attempted"
    child: subprocess.Popen[str] | None = None
    fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    previous_handler = signal.getsignal(signal.SIGIO)
    try:
        with pytest.raises(MigrationError, match="lease was broken"):
            with migration._source_read_lease(fd) as lease_broken:
                child = subprocess.Popen([
                    sys.executable,
                    "-c",
                    "import os, pathlib, sys; pathlib.Path(sys.argv[2]).touch(); fd = os.open(sys.argv[1], os.O_RDWR); os.write(fd, b'X'); os.close(fd)",
                    str(source),
                    str(attempted),
                ])
                deadline = time.monotonic() + 3
                while not attempted.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert attempted.exists(), "writer process did not attempt its O_RDWR open"
                while not lease_broken() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert lease_broken(), "kernel did not report the lease-break request"
        assert child is not None
        assert child.wait(timeout=3) == 0
        assert source.read_bytes().startswith(b"X")
    except MigrationError as exc:
        if "unavailable" in str(exc):
            pytest.skip("kernel lease protection is unavailable")
        raise
    finally:
        os.close(fd)
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=3)
    assert signal.getsignal(signal.SIGIO) is previous_handler

@pytest.mark.parametrize("mode", [0o770, 0o777, 0o1777])
def test_group_or_world_writable_scratch_is_rejected(tmp_path: Path, mode: int) -> None:
    scratch = _scratch(tmp_path); scratch.chmod(mode)
    with pytest.raises(MigrationError, match="scratch directory is unsafe"):
        migration._safe_scratch_dir(scratch)

def test_stage_replacement_cleanup_fails_closed(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    parent_fd, parent_identity = migration._open_safe_scratch_dir(scratch)
    stage = migration._create_stage(parent_fd, scratch)
    assert stage.parent_identity == parent_identity
    path = scratch / stage.stage_name
    moved = scratch / "retained-evidence"; path.rename(moved); path.mkdir()
    with pytest.raises(MigrationError, match="cleanup safety error"):
        migration._cleanup_stage(stage)
    assert moved.is_dir() and path.is_dir()
    moved.rmdir(); path.rmdir()

def _exact_acl_rows() -> list[dict[str, object]]:
    owner = 42
    return [{"kind": kind, "name": name, "owner": owner, "current_owner": owner, "acl_is_default": True}
            for kind, name in {("schema", "hermes_hot")} | {("table", name) for name in set(TABLE_COLUMNS) | {"migration_runs", "domain_manifests"}}]

def test_acl_catalog_requires_exact_default_for_every_expected_object() -> None:
    rows = _exact_acl_rows()
    assert migration._acls_are_exact_defaults(rows)
    for replacement in ({"acl_is_default": False}, {"owner": 99}, {"acl_is_default": None}):
        poisoned = [dict(row) for row in rows]
        poisoned[0].update(replacement)
        assert not migration._acls_are_exact_defaults(poisoned)
    assert not migration._acls_are_exact_defaults(rows[:-1])
    assert not migration._acls_are_exact_defaults([*rows, dict(rows[0])])

def test_acl_verifier_handles_public_catalog_grantee_without_pseudo_role_resolution() -> None:
    class FakeCatalog:
        async def fetch(self, query: str) -> list[dict[str, object]]:
            assert "acldefault" in query
            return [{"kind": "table", "name": "messages", "owner": 42, "current_owner": 42, "acl_is_default": False}]
    with pytest.raises(MigrationError, match="target schema mismatch"):
        asyncio.run(migration._verify_target_acls(FakeCatalog()))

def test_stage_is_private_and_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, logs = _fixture_dbs(tmp_path); hashes = _hashes(state, logs); scratch = _scratch(tmp_path)
    monkeypatch.setattr(migration.fcntl, "ioctl", lambda *args: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "unsupported")))
    preflight(state, logs, 1, scratch_dir=scratch, **hashes)
    assert not list(scratch.iterdir())

def test_reflink_is_attempted_without_full_copy_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, logs = _fixture_dbs(tmp_path); hashes = _hashes(state, logs)
    def reflink_copy(destination_fd: int, _request: int, source_fd: int) -> None:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while chunk := os.read(source_fd, 1024 * 1024):
            os.write(destination_fd, chunk)
    monkeypatch.setattr(migration.fcntl, "ioctl", reflink_copy)
    monkeypatch.setattr(migration.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 0})())
    preflight(state, logs, 1, scratch_dir=_scratch(tmp_path), **hashes)

def test_projected_storage_has_fixed_and_index_headroom() -> None:
    manifest = migration.DomainManifest("messages", (), "x", 2, 0, 100, None, None, None, None, "h")
    assert _projected_storage({"messages": manifest}, 1.5) == int((100 + 2 * 128) * 1.25 * 1.5)
    with pytest.raises(MigrationError): _projected_storage({"messages": manifest}, 1.49)

def test_hardlinked_source_is_rejected_before_staging(tmp_path: Path) -> None:
    state, logs = _fixture_dbs(tmp_path); os.link(state, tmp_path / "state-alias.db")
    with pytest.raises(MigrationError, match="singly linked"):
        preflight(state, logs, 1, scratch_dir=_scratch(tmp_path), **_hashes(state, logs))

def test_short_writes_are_retried_to_exact_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"; destination = tmp_path / "destination"; source.write_bytes(b"abcdef" * 100)
    original_write = migration.os.write
    monkeypatch.setattr(migration.fcntl, "ioctl", lambda *args: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "unsupported")))
    monkeypatch.setattr(migration.os, "write", lambda fd, data: original_write(fd, data[:max(1, len(data) // 3)]))
    with source.open("rb") as stream:
        migration._copy_or_reflink(stream.fileno(), destination, source.stat().st_size)
    assert _sha256_file(destination) == _sha256_file(source)

def test_message_manifest_bounds_are_numeric(tmp_path: Path) -> None:
    state, _ = _fixture_dbs(tmp_path); conn = sqlite3.connect(state)
    row = {x: None for x in TABLE_COLUMNS["messages"]}; row.update(id=10, session_id="s1", role="user", timestamp=3.0, active=1, compacted=0)
    conn.execute(f'INSERT INTO messages ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values())); conn.commit()
    manifest = migration._manifest(conn, "messages", 1, migration.DEFAULT_ROW_BYTES, "h")
    conn.close()
    assert (manifest.min_id, manifest.max_id) == (1, 10)


def test_public_approval_rejects_arbitrary_fixture_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, logs = _fixture_dbs(tmp_path)
    monkeypatch.setattr(migration, "_stage_sources", lambda *args, **kwargs: pytest.fail("public fixture reached staging"))
    with pytest.raises(MigrationError, match="state SHA256"):
        migration.preflight(state, logs, 1, scratch_dir=_scratch(tmp_path))


def test_public_preflight_routes_exact_production_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[migration.SnapshotApproval] = []
    def approved(*args: object, approval: migration.SnapshotApproval, **kwargs: object) -> str:
        captured.append(approval)
        return "sentinel"
    monkeypatch.setattr(migration, "_preflight_approved", approved)
    assert migration.preflight(tmp_path / "state", tmp_path / "logs", 1, scratch_dir=_scratch(tmp_path)) == "sentinel"
    assert captured == [migration.PRODUCTION_APPROVAL]


def test_public_migrate_routes_exact_production_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[migration.SnapshotApproval] = []
    async def approved(*args: object, approval: migration.SnapshotApproval, **kwargs: object) -> dict[str, object]:
        captured.append(approval)
        return {"ok": True}
    monkeypatch.setattr(migration, "_migrate_approved", approved)
    assert asyncio.run(migration.migrate(source=tmp_path / "state", agent_logs=tmp_path / "logs", scratch_dir=_scratch(tmp_path), cutoff=1)) == {"ok": True}
    assert captured == [migration.PRODUCTION_APPROVAL]


@pytest.mark.parametrize("cutoff", [float("nan"), float("inf"), float("-inf"), True])
def test_preflight_rejects_nonfinite_or_boolean_cutoff_before_io(tmp_path: Path, cutoff: object) -> None:
    state, logs = _fixture_dbs(tmp_path)
    with pytest.raises(MigrationError, match="invalid migration limits"):
        preflight(state, logs, cutoff, scratch_dir=_scratch(tmp_path), **_hashes(state, logs))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1.49, True])
def test_projected_storage_rejects_invalid_multiplier(value: object) -> None:
    manifest = migration.DomainManifest("messages", (), "x", 0, 0, 0, None, None, None, None, "h")
    with pytest.raises(MigrationError, match="invalid migration limits"):
        _projected_storage({"messages": manifest}, value)


@pytest.mark.parametrize(("name", "value"), [("row_byte_limit", 0), ("row_byte_limit", True),
    ("batch_byte_limit", -1), ("ceiling_bytes", True), ("safety_margin_bytes", -1)])
def test_migrate_rejects_invalid_byte_limits_before_target_io(tmp_path: Path, name: str, value: object) -> None:
    state, logs = _fixture_dbs(tmp_path)
    kwargs = {name: value}
    with pytest.raises(MigrationError, match="invalid migration limits"):
        asyncio.run(migrate(source=state, agent_logs=logs, scratch_dir=_scratch(tmp_path), target_dsn="not-a-dsn", cutoff=1, **kwargs, **_hashes(state, logs)))


def test_cli_invalid_cutoff_reads_no_secret_or_target_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class TrackingStdin:
        def readline(self) -> str:
            pytest.fail("invalid cutoff read stdin")
    monkeypatch.delenv("HERMES_POSTGRES_MIGRATION_DSN", raising=False)
    monkeypatch.setattr(migration.sys, "stdin", TrackingStdin())
    monkeypatch.setattr(migration, "parse_target_dsn", lambda *args, **kwargs: pytest.fail("invalid cutoff parsed DSN"))
    assert main(["--source", str(tmp_path / "state"), "--agent-logs", str(tmp_path / "logs"),
                 "--scratch-dir", str(_scratch(tmp_path)), "--cutoff", "nan"]) == 2
    assert capsys.readouterr().err == "migration failed: invalid migration limits\n"


@pytest.mark.skipif(not sys.platform.startswith("linux") or not hasattr(migration.fcntl, "F_SETLEASE"), reason="Linux leases unavailable")
def test_unrelated_sigio_is_chained_without_breaking_dedicated_lease(tmp_path: Path) -> None:
    source = tmp_path / "leased.sqlite"
    source.write_bytes(b"original")
    fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    calls: list[int] = []
    previous = signal.getsignal(signal.SIGIO)

    def on_sigio(signum: int, _frame: object) -> None:
        calls.append(signum)

    signal.signal(signal.SIGIO, on_sigio)
    try:
        try:
            with migration._source_read_lease(fd) as lease_broken:
                os.kill(os.getpid(), signal.SIGIO)
                assert calls == [signal.SIGIO]
                assert not lease_broken()
        except MigrationError as exc:
            if "unavailable" in str(exc):
                pytest.skip("kernel dedicated lease signal is unavailable")
            raise
    finally:
        signal.signal(signal.SIGIO, previous)
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("linux") or not hasattr(migration.fcntl, "F_SETLEASE"), reason="Linux leases unavailable")
def test_concurrent_lease_attempt_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "leased.sqlite"
    source.write_bytes(b"original")
    first, second = os.open(source, os.O_RDONLY | os.O_CLOEXEC), os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    try:
        try:
            with migration._source_read_lease(first):
                with pytest.raises(MigrationError, match="unavailable"):
                    with migration._source_read_lease(second):
                        pass
        except MigrationError as exc:
            if "unavailable" in str(exc):
                pytest.skip("kernel dedicated lease signal is unavailable")
            raise
    finally:
        os.close(first)
        os.close(second)


def test_cleanup_failure_preserves_primary_diagnosis(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    parent_fd, _ = migration._open_safe_scratch_dir(scratch)
    stage = migration._create_stage(parent_fd, scratch)
    (scratch / stage.stage_name / "unexpected").write_text("evidence")
    primary = MigrationError("primary hash mismatch")
    with pytest.raises(MigrationError, match="primary hash mismatch") as error:
        migration._cleanup_after_failure(stage, primary)
    assert "cleanup safety error; stage evidence retained" in "\n".join(error.value.__notes__)
    assert isinstance(error.value.__cause__, MigrationError)


@pytest.mark.parametrize(("failure", "expected", "stage_failure"), [
    (MigrationError("sanitized validation failed"), "sanitized validation failed", True),
    (RuntimeError("target operation boom"), "target migration failed", False),
])
def test_migration_failure_close_error_preserves_primary_and_cleans_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException, expected: str, stage_failure: bool,
) -> None:
    closed: list[str] = []
    cleaned: list[object] = []
    stage = types.SimpleNamespace(stage_fd=0)

    class Source:
        conn = object()
        def __init__(self, name: str) -> None: self.name = name
        def close(self) -> None: closed.append(self.name)
        def verify_unchanged(self, _hash: str) -> None: pass

    class Transaction:
        async def __aenter__(self) -> None: return None
        async def __aexit__(self, *args: object) -> bool: return False

    class Connection:
        def transaction(self) -> Transaction: return Transaction()
        async def close(self) -> None: raise RuntimeError("close boom")
        async def execute(self, _query: str) -> None: raise failure

    conn = Connection()
    async def connect(**_kwargs: object) -> Connection: return conn
    sources = iter((Source("state"), Source("logs")))
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(migration, "_verify_approved_sources", lambda *args: None)
    monkeypatch.setattr(migration, "_stage_sources", lambda *args, **kwargs: (stage, {}))
    monkeypatch.setattr(migration, "_open_source", lambda _path: next(sources))
    monkeypatch.setattr(migration, "_preflight_open", lambda *args, **kwargs: (("state", "logs"), {}))
    monkeypatch.setattr(migration, "_projected_storage", lambda *args: 0)
    def cleanup(value: object) -> None:
        cleaned.append(value)
        if stage_failure:
            raise MigrationError("stage boom")
    monkeypatch.setattr(migration, "_cleanup_stage", cleanup)
    with pytest.raises(MigrationError, match=expected) as error:
        asyncio.run(migrate(source=tmp_path / "state", agent_logs=tmp_path / "logs", scratch_dir=_scratch(tmp_path),
                            target=migration.TargetConfig("localhost", 5432, "u", "secret", "db", False),
                            cutoff=1, approval=migration.PRODUCTION_APPROVAL))
    assert closed == ["state", "logs"] and cleaned == [stage]
    notes = "\n".join(error.value.__notes__)
    assert "target connection close failed" in notes
    assert "close boom" not in str(error.value) + notes and "stage boom" not in str(error.value) + notes
    if stage_failure:
        assert "cleanup safety error; stage evidence retained" in notes
        assert isinstance(error.value.__cause__, MigrationError)


def test_committed_migration_close_error_reports_safe_replay_and_cleans_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    cleaned: list[object] = []
    stage = types.SimpleNamespace(stage_fd=0)

    class Source:
        conn = object()
        def __init__(self, name: str) -> None: self.name = name
        def close(self) -> None: closed.append(self.name)
        def verify_unchanged(self, _hash: str) -> None: pass

    class Transaction:
        async def __aenter__(self) -> None: return None
        async def __aexit__(self, *args: object) -> bool: return False

    class Connection:
        def transaction(self) -> Transaction: return Transaction()
        async def close(self) -> None: raise RuntimeError("close boom")
        async def execute(self, _query: str, *args: object) -> None: return None
        async def fetchval(self, _query: str, *args: object) -> int | bool: return False
        async def fetch(self, _query: str, *args: object) -> list[object]: return []
        async def fetchrow(self, _query: str, *args: object) -> None: return None

    async def connect(**_kwargs: object) -> Connection: return Connection()
    sources = iter((Source("state"), Source("logs")))
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(migration, "_verify_approved_sources", lambda *args: None)
    monkeypatch.setattr(migration, "_stage_sources", lambda *args, **kwargs: (stage, {}))
    monkeypatch.setattr(migration, "_open_source", lambda _path: next(sources))
    monkeypatch.setattr(migration, "_preflight_open", lambda *args, **kwargs: (("state", "logs"), {}))
    monkeypatch.setattr(migration, "_projected_storage", lambda *args: 0)
    monkeypatch.setattr(migration, "_verify_target_schema", lambda _conn: asyncio.sleep(0))
    monkeypatch.setattr(migration, "TABLE_COLUMNS", {})
    monkeypatch.setattr(migration, "_cleanup_stage", lambda value: cleaned.append(value))
    with pytest.raises(MigrationError, match="migration committed but local cleanup failed") as error:
        asyncio.run(migrate(source=tmp_path / "state", agent_logs=tmp_path / "logs", scratch_dir=_scratch(tmp_path),
                            target=migration.TargetConfig("localhost", 5432, "u", "secret", "db", False),
                            cutoff=1, approval=migration.PRODUCTION_APPROVAL))
    assert closed == ["state", "logs"] and cleaned == [stage]
    assert "target connection close failed" in "\n".join(error.value.__notes__)
    assert "close boom" not in str(error.value) + "\n".join(error.value.__notes__)


def test_bounded_target_close_timeout_terminates_and_preserves_primary_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    cleaned: list[object] = []
    stage = object()
    primary = MigrationError("precommit failure")

    class Source:
        def __init__(self, name: str) -> None: self.name = name
        def close(self) -> None: closed.append(self.name)

    class Connection:
        async def close(self, *, timeout: float | None = None) -> None:
            assert timeout == migration.TARGET_CLOSE_TIMEOUT_SECONDS
            raise TimeoutError("target details must not leak")
        def terminate(self) -> None: closed.append("target-terminated")

    monkeypatch.setattr(migration, "_cleanup_stage", lambda value: cleaned.append(value))
    asyncio.run(migration._finalize_migration_resources(Connection(), Source("state"), Source("logs"), stage, primary=primary))
    assert closed == ["target-terminated", "state", "logs"]
    assert cleaned == [stage]
    notes = "\n".join(primary.__notes__)
    assert "target connection close failed" in notes
    assert "target details" not in notes


def test_bounded_target_close_finishes_despite_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stage = object()

    class Source:
        def __init__(self, name: str) -> None: self.name = name
        def close(self) -> None: events.append(self.name)

    class Connection:
        async def close(self, *, timeout: float | None = None) -> None:
            assert timeout == 0.01
            await asyncio.wait_for(asyncio.Event().wait(), timeout=timeout)
        def terminate(self) -> None: events.append("target-terminated")

    async def exercise() -> None:
        current = asyncio.current_task()
        assert current is not None
        loop = asyncio.get_running_loop()
        loop.call_soon(current.cancel)
        loop.call_later(0.002, current.cancel)
        with pytest.raises(MigrationError, match="migration committed but local cleanup failed"):
            await migration._finish_migration_cleanup(
                migration._finalize_migration_resources(Connection(), Source("state"), Source("logs"), stage),
            )

    monkeypatch.setattr(migration, "TARGET_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(migration, "_cleanup_stage", lambda value: events.append("stage"))
    asyncio.run(asyncio.wait_for(exercise(), timeout=0.5))
    assert events == ["target-terminated", "state", "logs", "stage"]


def test_target_terminate_failure_is_sanitized_and_cleanup_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    cleaned: list[object] = []
    stage = object()
    primary = MigrationError("precommit failure")

    class Source:
        def __init__(self, name: str) -> None: self.name = name
        def close(self) -> None: closed.append(self.name)

    class Connection:
        async def close(self, *, timeout: float | None = None) -> None: raise RuntimeError("close secret")
        def terminate(self) -> None: raise RuntimeError("terminate secret")

    monkeypatch.setattr(migration, "_cleanup_stage", lambda value: cleaned.append(value))
    asyncio.run(migration._finalize_migration_resources(Connection(), Source("state"), Source("logs"), stage, primary=primary))
    assert closed == ["state", "logs"] and cleaned == [stage]
    notes = "\n".join(primary.__notes__)
    assert "target connection close failed" in notes
    assert "target connection terminate failed" in notes
    assert "secret" not in notes


@pytest.mark.parametrize("where", ["operation", "transaction_exit"])
@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_base_exception_finalizes_once_and_preserves_replay_safe_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, failure_type: type[BaseException],
) -> None:
    closed: list[str] = []
    cleaned: list[object] = []
    stage = types.SimpleNamespace(stage_fd=0)
    failure = failure_type()

    class Source:
        conn = object()
        def __init__(self, name: str) -> None: self.name = name
        def close(self) -> None: closed.append(self.name)
        def verify_unchanged(self, _hash: str) -> None: pass

    class Transaction:
        async def __aenter__(self) -> None: return None
        async def __aexit__(self, *args: object) -> bool:
            if where == "transaction_exit": raise failure
            return False

    class Connection:
        def transaction(self) -> Transaction: return Transaction()
        async def close(self) -> None:
            closed.append("target")
            raise asyncio.CancelledError()
        async def execute(self, _query: str, *args: object) -> None:
            if where == "operation": raise failure
        async def fetchval(self, _query: str, *args: object) -> int | bool: return False
        async def fetch(self, _query: str, *args: object) -> list[object]: return []
        async def fetchrow(self, _query: str, *args: object) -> None: return None

    async def connect(**_kwargs: object) -> Connection: return Connection()
    sources = iter((Source("state"), Source("logs")))
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(migration, "_verify_approved_sources", lambda *args: None)
    monkeypatch.setattr(migration, "_stage_sources", lambda *args, **kwargs: (stage, {}))
    monkeypatch.setattr(migration, "_open_source", lambda _path: next(sources))
    monkeypatch.setattr(migration, "_preflight_open", lambda *args, **kwargs: (("state", "logs"), {}))
    monkeypatch.setattr(migration, "_projected_storage", lambda *args: 0)
    monkeypatch.setattr(migration, "_verify_target_schema", lambda _conn: asyncio.sleep(0))
    monkeypatch.setattr(migration, "TABLE_COLUMNS", {})
    monkeypatch.setattr(migration, "_cleanup_stage", lambda value: cleaned.append(value))
    with pytest.raises(failure_type) as error:
        asyncio.run(migrate(source=tmp_path / "state", agent_logs=tmp_path / "logs", scratch_dir=_scratch(tmp_path),
                            target=migration.TargetConfig("localhost", 5432, "u", "secret", "db", False),
                            cutoff=1, approval=migration.PRODUCTION_APPROVAL))
    assert error.value is failure
    assert closed == ["target", "state", "logs"] and cleaned == [stage]
    notes = "\n".join(error.value.__notes__)
    assert "migration cancelled/interrupted; commit outcome may be ambiguous; exact replay is safe" in notes
    assert "target connection close failed" in notes
    assert "secret" not in notes


def test_cleanup_waiter_survives_repeated_cancellation_without_orphaning() -> None:
    async def exercise() -> list[str]:
        events: list[str] = []
        release = asyncio.Event()
        current = asyncio.current_task()
        assert current is not None

        async def cleanup() -> None:
            events.append("started")
            loop = asyncio.get_running_loop()
            loop.call_soon(current.cancel)
            loop.call_soon(current.cancel)
            loop.call_later(0.002, current.cancel)
            loop.call_later(0.01, release.set)
            await release.wait()
            events.append("finished")

        await migration._finish_migration_cleanup(cleanup())
        return events

    assert asyncio.run(exercise()) == ["started", "finished"]
