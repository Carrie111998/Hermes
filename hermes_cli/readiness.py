"""Project go-live readiness report command."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
import requests
import yaml



def _run_git_command(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Git command failed: "
            + " ".join(arguments)
            + (f": {message}" if message else "")
        )

    return result.stdout.rstrip("\n")


def _collect_git_information(repository: Path) -> dict[str, object]:
    status_output = _run_git_command(repository, "status", "--short")
    status_lines = tuple(
        line for line in status_output.splitlines() if line.strip()
    )

    branch = _run_git_command(repository, "branch", "--show-current")
    head = _run_git_command(repository, "rev-parse", "HEAD")
    remote = _run_git_command(repository, "remote", "get-url", "origin")

    default_branch = None
    upstream = None
    ahead = None
    behind = None

    try:
        text = subprocess.run(
            ("git","-C",str(repository),"remote","show","origin"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        for row in text.splitlines():
            if "HEAD branch:" in row:
                default_branch = row.split(":",1)[1].strip()
                break
    except Exception:
        pass

    try:
        upstream = _run_git_command(
            repository,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{u}",
        )

        counts = _run_git_command(
            repository,
            "rev-list",
            "--left-right",
            "--count",
            "@{u}...HEAD",
        ).split()

        if len(counts) == 2:
            behind = int(counts[0])
            ahead = int(counts[1])

    except Exception:
        pass

    return {
        "branch": branch,
        "head": head,
        "remote": remote,
        "default_branch": default_branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "clean": not status_lines,
        "status": status_lines,
    }

def _collect_test_information(repository: Path) -> dict[str, object]:
    pytest_ini = (repository / "pytest.ini").exists()

    pyproject = repository / "pyproject.toml"
    has_pytest = False

    if pyproject.exists():
        data = pyproject.read_text(encoding="utf-8", errors="ignore")
        has_pytest = "pytest" in data

    test_count = sum(1 for _ in repository.rglob("test_*.py"))

    return {
        "framework": "pytest" if (pytest_ini or has_pytest) else "unknown",
        "pytest_ini": pytest_ini,
        "test_files": test_count,
    }


def _collect_module_information(repository: Path) -> dict[str, object]:
    package_directories = sorted(
        path.parent
        for path in repository.rglob("__init__.py")
        if ".git" not in path.parts
        and "venv" not in path.parts
        and "node_modules" not in path.parts
    )

    python_files = sum(
        1
        for path in repository.rglob("*.py")
        if ".git" not in path.parts
        and "venv" not in path.parts
        and "node_modules" not in path.parts
    )

    return {
        "package_count": len(package_directories),
        "python_files": python_files,
    }


def _collect_api_information(repository: Path) -> dict[str, object]:
    python_files = tuple(repository.rglob("*.py"))

    fastapi_routes = 0
    flask_routes = 0
    routers = 0

    for file in python_files:
        try:
            data = file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        fastapi_routes += data.count("@app.")
        fastapi_routes += data.count("@router.")

        flask_routes += data.count("@bp.route")
        flask_routes += data.count("@app.route")

        routers += data.count("APIRouter(")

    return {
        "python_files": len(python_files),
        "fastapi_routes": fastapi_routes,
        "flask_routes": flask_routes,
        "routers": routers,
    }


def _load_readiness_configuration(repository: Path) -> dict[str, object]:
    configuration_path = repository / "readiness.yaml"
    if not configuration_path.exists():
        configuration_path = repository / "readiness.yml"
        if not configuration_path.exists():
            return {}

    try:
        loaded = yaml.safe_load(configuration_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"Readiness configuration could not be loaded: {exc}"
        ) from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError("Readiness configuration must be a mapping")
    return loaded



def _json_path_value(document: dict[str, object], path: str):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value

def _collect_http_health_information(repository: Path) -> dict[str, object]:
    configuration = _load_readiness_configuration(repository)
    http = configuration.get("http", {})
    if not isinstance(http, dict):
        raise RuntimeError("http must be a mapping")
    base_url = http.get("base_url")
    http_checks = http.get("checks", [])

    if not http_checks:
        return {
            "configured": False,
            "http_ok": False,
            "health_ok": False,
            "service": None,
            "checks": (),
        }

    if not isinstance(base_url, str) or not base_url.strip():
        raise RuntimeError(
            "readiness.yaml requires base_url when http_checks are configured"
        )
    if not isinstance(http_checks, list):
        raise RuntimeError("http_checks must be a list")

    results: list[dict[str, object]] = []
    service = None

    for index, check in enumerate(http_checks, start=1):
        if not isinstance(check, dict):
            raise RuntimeError(f"http_checks[{index}] must be a mapping")

        path = check.get("path", "/")
        expected_status = check.get("expect", 200)
        expected_json = check.get("json", {})
        timeout = check.get("timeout", 5)

        if not isinstance(path, str) or not path.startswith("/"):
            raise RuntimeError(f"http_checks[{index}].path must start with /")
        if not isinstance(expected_status, int):
            raise RuntimeError(f"http_checks[{index}].expect must be an integer")
        if not isinstance(expected_json, dict):
            raise RuntimeError(f"http_checks[{index}].json must be a mapping")

        url = base_url.rstrip("/") + path
        status_code = None
        response_json: dict[str, object] = {}
        error = None

        try:
            response = requests.get(url, timeout=float(timeout))
            status_code = response.status_code
            if expected_json:
                parsed = response.json()
                if isinstance(parsed, dict):
                    response_json = parsed
        except (requests.RequestException, ValueError) as exc:
            error = str(exc)

        status_ok = status_code == expected_status
        json_ok = all(
            _json_path_value(response_json, key) == expected_value
            for key, expected_value in expected_json.items()
        )
        check_ok = error is None and status_ok and json_ok

        candidate = (
            _json_path_value(response_json,"data.service")
            or _json_path_value(response_json,"service")
        )
        if service is None and isinstance(candidate, str):
            service = candidate

        results.append({
            "path": path,
            "url": url,
            "expected_status": expected_status,
            "status_code": status_code,
            "status_ok": status_ok,
            "json_ok": json_ok,
            "ok": check_ok,
            "error": error,
        })

    return {
        "configured": True,
        "http_ok": all(result["error"] is None for result in results),
        "health_ok": all(bool(result["ok"]) for result in results),
        "service": service,
        "checks": tuple(results),
    }



def _collect_systemd_information() -> dict[str, object]:
    import subprocess

    services = (
        "agakoc-hermes-api.service",
        "agakoc-hermes-mcp.service",
    )

    result = []

    for service in services:
        values = {}

        proc = subprocess.run(
            (
                "systemctl",
                "show",
                service,
                "-p","Id",
                "-p","ActiveState",
                "-p","SubState",
                "-p","ExecMainStatus",
                "-p","NRestarts",
                "-p","UnitFileState",
            ),
            capture_output=True,
            text=True,
        )

        for line in proc.stdout.splitlines():
            if "=" in line:
                k,v=line.split("=",1)
                values[k]=v

        result.append(values)

    return {
        "services": result,
        "healthy": all(
            s.get("ActiveState")=="active"
            and s.get("ExecMainStatus")=="0"
            for s in result
        ),
    }


def _collect_docker_information() -> dict[str, object]:
    import json
    import subprocess

    proc=subprocess.run(
        ("docker","ps","--format","{{json .}}"),
        capture_output=True,
        text=True,
    )

    containers=[]

    for line in proc.stdout.splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except Exception:
            pass

    return {
        "running":len(containers),
        "containers":containers,
    }


def _calculate_readiness_score(
    git_information: dict[str, object],
    test_information: dict[str, object],
    module_information: dict[str, object],
    api_information: dict[str, object],
    http_information: dict[str, object],
) -> dict[str, object]:
    score = 100
    findings = []

    if not git_information["clean"]:
        score -= 20
        findings.append("Working Tree nicht sauber")

    if test_information["framework"] == "unknown":
        score -= 20
        findings.append("Kein Testframework erkannt")

    if module_information["python_files"] == 0:
        score -= 10
        findings.append("Keine Module erkannt")

    if api_information["python_files"] == 0:
        score -= 10
        findings.append("Keine Python-Dateien erkannt")

    if not http_information["health_ok"]:
        score -= 20
        findings.append("HTTP-Healthcheck fehlgeschlagen")

    score = max(score, 0)

    return {
        "score": score,
        "ready": score >= 90,
        "findings": findings,
    }




def _render_markdown(
    repository: Path,
    generated_at: str,
    git_information: dict[str, object],
    test_information: dict[str, object],
    module_information: dict[str, object],
    api_information: dict[str, object],
    http_information: dict[str, object],
    readiness_score: dict[str, object],
) -> str:
    return "\n".join(
        (
            "# Go-Live Readiness",
            "",
            f"- Projekt: `{repository.name}`",
            f"- Repository: `{repository}`",
            f"- Erzeugt: `{generated_at}`",
            "- Status: `inventory_pending`",
            "",
            "## Git",
            "",
            f"- Branch: `{git_information['branch']}`",
            f"- HEAD: `{git_information['head']}`",
            f"- Remote: `{git_information['remote']}`",
            f"- Working Tree sauber: `{git_information['clean']}`",
            "",
            "## Tests",
            "",
            f"- Framework: `{test_information['framework']}`",
            f"- Testdateien: `{test_information['test_files']}`",
            f"- pytest.ini: `{test_information['pytest_ini']}`",
            "",
            "## Module",
            "",
            f"- Python-Pakete: `{module_information['package_count']}`",
            f"- Python-Dateien: `{module_information['python_files']}`",
            "",
            "## Go-Live Readiness",
            f"- Score: `{readiness_score['score']}`",
            f"- Bereit: `{readiness_score['ready']}`",
            "",
            "### Feststellungen",
            "",
            *(readiness_score["findings"] or ("Keine Feststellungen.",)),
            "",
            "## HTTP",
            "",
            f"- Health erreichbar: {http_information['health_ok']}",
            f"- Service: {http_information['service']}",
            "",
            "## API",
            "",
            f"- Python-Dateien: `{api_information['python_files']}`",
            f"- FastAPI-Routen: `{api_information['fastapi_routes']}`",
            f"- Flask-Routen: `{api_information['flask_routes']}`",
            f"- APIRouter: `{api_information['routers']}`",
            "",
            "### Änderungen",
            "",
            *(git_information["status"] or ("Keine Änderungen.",)),
            "",
        )
    )




def _render_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
    ) + "\n"


def readiness_command(args) -> int:
    """Validate the repository and create initial readiness reports."""
    repository = Path(args.repo).expanduser().resolve()
    output_directory = Path(args.output).expanduser().resolve()

    if not repository.is_dir():
        raise SystemExit(f"Repository does not exist: {repository}")

    if not (repository / ".git").exists():
        raise SystemExit(f"Not a Git repository: {repository}")

    output_directory.mkdir(parents=True, exist_ok=True)

    git_information = _collect_git_information(repository)
    test_information = _collect_test_information(repository)
    module_information = _collect_module_information(repository)
    api_information = _collect_api_information(repository)
    http_information = _collect_http_health_information(repository)
    systemd_information = _collect_systemd_information()
    docker_information = _collect_docker_information()

    readiness_score = _calculate_readiness_score(
        git_information,
        test_information,
        module_information,
        api_information,
        http_information,
    )

    formats = tuple(dict.fromkeys(args.format or ("markdown",)))
    generated_at = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": 1,
        "project": repository.name,
        "repository": str(repository),
        "generated_at": generated_at,
        "status": "inventory_pending",
        "git": git_information,
        "tests": test_information,
        "modules": module_information,
        "api": api_information,
        "http": http_information,
        "systemd": systemd_information,
        "docker": docker_information,
        "readiness": readiness_score,
    }

    generated_files: list[Path] = []

    if "markdown" in formats:
        markdown_path = output_directory / "GO_LIVE_READINESS.md"
        markdown_path.write_text(
            _render_markdown(
                repository,
                generated_at,
                git_information,
                test_information,
                module_information,
                api_information,
                http_information,
                readiness_score,
            ),

            encoding="utf-8",
        )
        generated_files.append(markdown_path)

    if "json" in formats:
        json_path = output_directory / "GO_LIVE_READINESS.json"
        json_path.write_text(
            _render_json(payload),
            encoding="utf-8",
        )
        generated_files.append(json_path)

    for generated_file in generated_files:
        print(generated_file)

    return 0
