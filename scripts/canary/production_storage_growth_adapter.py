#!/usr/bin/env python3
"""Concrete fixed GCE + IAP adapter for production storage growth.

This adapter deliberately has no generic compute method and no generic SSH
method.  Its public surface is exactly the protocol required by
``ProductionStorageGrowthExecutor``.
"""

from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract
from scripts.canary import production_storage_growth_guest as guest


COMPUTE_API = "https://compute.googleapis.com/compute/v1"
GUEST_ENTRYPOINT = "/usr/local/lib/muncho/production-storage-growth-guest"


class ProductionStorageAdapterError(RuntimeError):
    """Stable, secret-free adapter failure."""


class FixedProductionComputeClient:
    """Compute REST client with one fixed instance, disk, and resize target."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        account_provider: Callable[[], str],
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not all(
            callable(value)
            for value in (token_provider, account_provider, urlopen, sleep)
        ):
            raise ProductionStorageAdapterError(
                "production_storage_compute_configuration_invalid"
            )
        self._token_provider = token_provider
        self._account_provider = account_provider
        self._urlopen = urlopen
        self._sleep = sleep

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if (
            method not in {"GET", "POST"}
            or not path.startswith(
                f"/projects/{contract.PROJECT}/zones/{contract.ZONE}/"
            )
            or self._account_provider() != contract.AUTHENTICATED_ACCOUNT
        ):
            raise ProductionStorageAdapterError(
                "production_storage_compute_boundary_invalid"
            )
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise ProductionStorageAdapterError(
                "production_storage_compute_authentication_failed"
            )
        data = None if body is None else protocol.canonical_json_bytes(body)
        request = urllib.request.Request(
            f"{COMPUTE_API}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._urlopen(request, timeout=30) as response:
                value = json.loads(response.read().decode("utf-8", errors="strict"))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ):
            raise ProductionStorageAdapterError(
                "production_storage_compute_request_failed"
            ) from None
        if not isinstance(value, Mapping):
            raise ProductionStorageAdapterError(
                "production_storage_compute_response_invalid"
            )
        return copy.deepcopy(dict(value))

    def get_instance(self) -> Mapping[str, Any]:
        return self._request(
            "GET",
            f"/projects/{contract.PROJECT}/zones/{contract.ZONE}/instances/"
            f"{contract.INSTANCE_NAME}",
        )

    def get_disk(self) -> Mapping[str, Any]:
        return self._request(
            "GET",
            f"/projects/{contract.PROJECT}/zones/{contract.ZONE}/disks/"
            f"{contract.DISK_NAME}",
        )

    def resize_once(self, *, provider_request_id: str) -> Mapping[str, Any]:
        path = (
            f"/projects/{contract.PROJECT}/zones/{contract.ZONE}/disks/"
            f"{contract.DISK_NAME}/resize?requestId="
            f"{urllib.parse.quote(provider_request_id, safe='')}"
        )
        operation = self._request(
            "POST", path, body={"sizeGb": str(contract.TARGET_SIZE_GB)}
        )
        operation_name = operation.get("name")
        if (
            not isinstance(operation_name, str)
            or not operation_name
            or operation.get("operationType") != "resize"
            or str(operation.get("targetId")) != contract.DISK_ID
        ):
            raise ProductionStorageAdapterError(
                "production_storage_resize_operation_invalid"
            )
        for _ in range(60):
            checked = self.get_disk()
            if (
                str(checked.get("id")) == contract.DISK_ID
                and int(str(checked.get("sizeGb", "0")))
                == contract.TARGET_SIZE_GB
                and checked.get("status") == "READY"
            ):
                return {
                    "accepted": True,
                    "provider_request_id": provider_request_id,
                    "operation_id": str(operation.get("id", "")),
                }
            self._sleep(2.0)
        raise ProductionStorageAdapterError(
            "production_storage_resize_operation_timeout"
        )


class FixedProductionIapGuestClient:
    """One fixed IAP destination and one fixed installed guest entrypoint."""

    def __init__(
        self, *, invoke_fixed_guest: Callable[[bytes], bytes]
    ):
        if not callable(invoke_fixed_guest):
            raise ProductionStorageAdapterError(
                "production_storage_guest_transport_configuration_invalid"
            )
        self._invoke_fixed_guest = invoke_fixed_guest

    def _invoke(self, operation: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {"readiness", "observe", "grow"}:
            raise ProductionStorageAdapterError(
                "production_storage_guest_operation_forbidden"
            )
        unsigned = {
            "schema": guest.REQUEST_SCHEMA,
            "operation": operation,
            "document": copy.deepcopy(dict(document)),
        }
        frame = {**unsigned, "request_sha256": protocol.sha256_json(unsigned)}
        raw = self._invoke_fixed_guest(protocol.canonical_json_bytes(frame))
        try:
            response = protocol.decode_canonical_json(raw.strip())
        except protocol.PasskeyV2ProtocolError:
            raise ProductionStorageAdapterError(
                "production_storage_guest_response_invalid"
            ) from None
        if not isinstance(response, Mapping):
            raise ProductionStorageAdapterError(
                "production_storage_guest_response_invalid"
            )
        unsigned_response = {
            name: item for name, item in response.items() if name != "response_sha256"
        }
        if (
            set(response)
            != {"schema", "operation", "ok", "document", "response_sha256"}
            or response.get("schema") != guest.RESPONSE_SCHEMA
            or response.get("operation") != operation
            or response.get("ok") is not True
            or not isinstance(response.get("document"), Mapping)
            or response.get("response_sha256")
            != protocol.sha256_json(unsigned_response)
        ):
            raise ProductionStorageAdapterError(
                "production_storage_guest_response_invalid"
            )
        return copy.deepcopy(dict(response["document"]))

    def observe(self) -> Mapping[str, Any]:
        return self._invoke("observe", {})

    def readiness(self) -> Mapping[str, Any]:
        return self._invoke("readiness", {})

    def grow(self, *, idempotency_key_sha256: str) -> Mapping[str, Any]:
        return self._invoke("grow", {"idempotency_key_sha256": idempotency_key_sha256})


def build_exact_observation(
    *,
    instance: Mapping[str, Any],
    disk: Mapping[str, Any],
    guest_facts: Mapping[str, Any],
    collected_at_unix: int,
) -> Mapping[str, Any]:
    """Build one canonical observation from fixed read-only source facts."""

    if (
        not isinstance(instance, Mapping)
        or not isinstance(disk, Mapping)
        or not isinstance(guest_facts, Mapping)
        or type(collected_at_unix) is not int
        or collected_at_unix <= 0
    ):
        raise ProductionStorageAdapterError(
            "production_storage_cloud_identity_invalid"
        )
    attachments = instance.get("disks")
    boot = (
        [
            item
            for item in attachments
            if isinstance(item, Mapping) and item.get("boot")
        ]
        if isinstance(attachments, list)
        else []
    )
    source_image = str(disk.get("sourceImage", ""))
    if len(boot) != 1:
        raise ProductionStorageAdapterError(
            "production_storage_cloud_identity_invalid"
        )
    attachment = boot[0]
    try:
        return contract.build_observation(
            collected_at_unix=collected_at_unix,
            authenticated_account=contract.AUTHENTICATED_ACCOUNT,
            impersonated_service_account=None,
            project=contract.PROJECT,
            zone=contract.ZONE,
            instance={
                "name": instance["name"],
                "id": str(instance["id"]),
                "status": instance["status"],
                "zone": str(instance["zone"]).rsplit("/", 1)[-1],
                "self_link": instance["selfLink"],
                "boot_disk_count": len(boot),
            },
            disk={
                "name": disk["name"],
                "id": str(disk["id"]),
                "type": str(disk["type"]).rsplit("/", 1)[-1],
                "size_gb": int(disk["sizeGb"]),
                "zone": str(disk["zone"]).rsplit("/", 1)[-1],
                "self_link": disk["selfLink"],
                "users": disk["users"],
                "status": disk["status"],
                "source_image_project": source_image.split("/projects/", 1)[
                    1
                ].split("/", 1)[0],
                "source_image_name": source_image.rsplit("/", 1)[-1],
            },
            boot_attachment={
                "boot": attachment["boot"],
                "auto_delete": attachment["autoDelete"],
                "device_name": attachment["deviceName"],
                "mode": attachment["mode"],
                "type": attachment["type"],
                "source": attachment["source"],
            },
            guest=guest_facts,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        contract.ProductionStorageGrowthError,
    ):
        raise ProductionStorageAdapterError(
            "production_storage_cloud_identity_invalid"
        ) from None


class FixedProductionStorageAdapter:
    """Exact executor transport assembled from the fixed cloud and IAP clients."""

    def __init__(
        self,
        *,
        growth_plan: Mapping[str, Any],
        cloud: FixedProductionComputeClient,
        guest_client: FixedProductionIapGuestClient,
        wall_clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        try:
            plan = contract.validate_plan(growth_plan)
        except contract.ProductionStorageGrowthError:
            raise ProductionStorageAdapterError(
                "production_storage_adapter_plan_invalid"
            ) from None
        if (
            not isinstance(cloud, FixedProductionComputeClient)
            or not isinstance(guest_client, FixedProductionIapGuestClient)
            or not callable(wall_clock)
        ):
            raise ProductionStorageAdapterError(
                "production_storage_adapter_configuration_invalid"
            )
        self._plan = plan
        self._cloud = cloud
        self._guest = guest_client
        self._wall_clock = wall_clock

    def observe_exact_target(self) -> Mapping[str, Any]:
        instance = self._cloud.get_instance()
        disk = self._cloud.get_disk()
        guest_facts = self._guest.observe()
        try:
            collected_at_unix = int(self._wall_clock())
        except (TypeError, ValueError):
            raise ProductionStorageAdapterError(
                "production_storage_cloud_identity_invalid"
            ) from None
        return build_exact_observation(
            instance=instance,
            disk=disk,
            guest_facts=guest_facts,
            collected_at_unix=collected_at_unix,
        )

    def resize_exact_disk_once(self, *, provider_request_id: str) -> Mapping[str, Any]:
        if provider_request_id != self._plan["provider_request_id"]:
            raise ProductionStorageAdapterError(
                "production_storage_provider_request_id_invalid"
            )
        return self._cloud.resize_once(provider_request_id=provider_request_id)

    def grow_exact_root_online(
        self, *, idempotency_key_sha256: str
    ) -> Mapping[str, Any]:
        if idempotency_key_sha256 != self._plan["idempotency_key_sha256"]:
            raise ProductionStorageAdapterError(
                "production_storage_idempotency_key_invalid"
            )
        return self._guest.grow(idempotency_key_sha256=idempotency_key_sha256)


__all__ = [
    "build_exact_observation",
    "FixedProductionComputeClient",
    "FixedProductionIapGuestClient",
    "FixedProductionStorageAdapter",
    "ProductionStorageAdapterError",
]
