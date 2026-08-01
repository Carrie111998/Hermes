from __future__ import annotations

from pathlib import Path
import tomllib

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
REQUIREMENTS = (
    REPO_ROOT
    / "plugins"
    / "skyai_customer"
    / "requirements-discord-mirror.txt"
)
DEPLOY_README = (
    REPO_ROOT / "plugins" / "skyai_customer" / "deploy" / "README.md"
)
FUTURE_MANIFEST = (
    REPO_ROOT
    / "plugins"
    / "skyai_customer"
    / "deploy"
    / "future-cloud-run-app-runtime.template.yaml"
)
SYSTEMD_DROPIN = (
    REPO_ROOT
    / "plugins"
    / "skyai_customer"
    / "deploy"
    / "skyai-v2-hermes-prod.service.d"
    / "20-production-gateway.conf.template"
)


def test_release_package_declares_exact_skyai_postgres_extra() -> None:
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)
    extras = project["project"]["optional-dependencies"]

    assert extras["skyai-discord-mirror"] == ["psycopg[binary]==3.2.9"]
    assert REQUIREMENTS.read_text(encoding="utf-8").splitlines()[-1] == (
        "psycopg[binary]==3.2.9"
    )


def test_frozen_runtime_lock_contains_exact_binary_driver() -> None:
    with UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages = {
        package["name"]: package
        for package in lock["package"]
    }

    assert packages["psycopg"]["version"] == "3.2.9"
    assert packages["psycopg-binary"]["version"] == "3.2.9"
    assert {
        dependency["name"]
        for dependency in packages["psycopg"]["dependencies"]
    } >= {"typing-extensions"}
    assert {
        dependency["name"]
        for dependency in packages["psycopg"][
            "optional-dependencies"
        ]["binary"]
    } == {"psycopg-binary"}


def test_future_cloud_run_reference_is_explicitly_inert() -> None:
    manifest = yaml.safe_load(FUTURE_MANIFEST.read_text(encoding="utf-8"))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    environment = {
        entry["name"]: entry
        for entry in container["env"]
    }

    assert manifest["apiVersion"] == "serving.knative.dev/v1"
    assert manifest["kind"] == "Service"
    assert manifest["metadata"]["name"] == (
        "SKYAI_FUTURE_APP_SERVICE_NAME_MUST_BE_BOUND"
    )
    assert manifest["metadata"]["annotations"]["adventico.ai/lifecycle"] == (
        "future-inert"
    )
    assert container["image"] == "SKYAI_IMAGE_DIGEST_MUST_BE_BOUND"
    assert container["command"] == ["python"]
    assert container["args"] == [
        "-m",
        "plugins.skyai_customer.production_gateway",
    ]
    assert container["startupProbe"]["httpGet"]["path"] == "/ready"
    assert (
        manifest["spec"]["template"]["metadata"]["annotations"][
            "autoscaling.knative.dev/minScale"
        ]
        == "1"
    )
    assert (
        manifest["spec"]["template"]["metadata"]["annotations"][
            "run.googleapis.com/vpc-access-connector"
        ]
        == "SKYAI_VPC_CONNECTOR_MUST_BE_BOUND"
    )
    assert (
        manifest["spec"]["template"]["metadata"]["annotations"][
            "run.googleapis.com/vpc-access-egress"
        ]
        == "private-ranges-only"
    )
    assert (
        manifest["spec"]["template"]["spec"]["serviceAccountName"]
        == "SKYAI_SERVICE_ACCOUNT_MUST_BE_BOUND"
    )

    assert environment["SKYAI_RUNTIME_MODE"]["value"] == "production"
    assert environment["SKYAI_DISCORD_MIRROR_ENABLED"]["value"] == "1"
    assert (
        environment["SKYAI_DISCORD_MIRROR_CREATE_THREADS"]["value"]
        == "1"
    )
    assert environment["SKYAI_DISCORD_MIRROR_CHANNEL_ID"]["value"] == (
        "1510888721614901358"
    )
    for secret_name in (
        "SKYAI_V2_CANARY_TOKEN",
        "SKYAI_DISCORD_BOT_TOKEN",
        "SKYAI_DISCORD_MIRROR_DATABASE_URL",
    ):
        secret_ref = environment[secret_name]["valueFrom"]["secretKeyRef"]
        assert secret_ref["key"] == "latest"
        assert secret_ref["name"].endswith("_MUST_BE_BOUND")


def test_current_vm_dropin_wires_only_production_entrypoint() -> None:
    text = SYSTEMD_DROPIN.read_text(encoding="utf-8")

    assert "[Service]" in text
    assert "WorkingDirectory=SKYAI_ACTIVE_RELEASE_MUST_BE_BOUND" in text
    assert "EnvironmentFile=SKYAI_PRODUCTION_ENV_FILE_MUST_BE_BOUND" in text
    assert "ExecStart=\n" in text
    assert (
        "ExecStart=SKYAI_SERVICE_PYTHON_MUST_BE_BOUND -m "
        "plugins.skyai_customer.production_gateway"
    ) in text
    assert "--dev" not in text


def test_current_topology_is_not_misidentified_as_cloud_run_app() -> None:
    text = DEPLOY_README.read_text(encoding="utf-8")

    assert "`skyai-prod-ingress`" in text
    assert "Cloud Run **proxy only**" in text
    assert "skyai-runtime-prod-01" in text
    assert "skyai-v2-hermes-prod.service" in text
    assert "SKYAI_PRODUCTION_BIND_HOST" in text
    assert "SKYAI_TRUSTED_PROXY_CIDR" in text
    assert "X-Forwarded-For" in text
    assert "root Dockerfile as the current PROD application build" in text
    assert "must never be applied to `skyai-prod-ingress`" in text


def test_raw_future_template_retains_unbound_release_sentinels() -> None:
    text = FUTURE_MANIFEST.read_text(encoding="utf-8")

    assert "SKYAI_FUTURE_APP_SERVICE_NAME_MUST_BE_BOUND" in text
    assert "SKYAI_IMAGE_DIGEST_MUST_BE_BOUND" in text
    assert "SKYAI_SERVICE_ACCOUNT_MUST_BE_BOUND" in text
    assert "SKYAI_VPC_CONNECTOR_MUST_BE_BOUND" in text
    assert "SKYAI_ABSOLUTE_PROFILE_HOME_MUST_BE_BOUND" in text
    assert "SKYAI_BUILD_COMMIT_MUST_BE_BOUND" in text
    assert text.count("_SECRET_MUST_BE_BOUND") == 3
