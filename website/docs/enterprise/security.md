---
sidebar_position: 5
title: "Security Model"
description: "Hermes Enterprise trust boundaries, secret brokering, sandbox verification, audit guarantees, and explicit v1 exclusions"
---

# Security Model

:::warning Status: draft / under development

This section documents Hermes Enterprise while its components are still landing as in-flight pull requests. Interfaces, commands, and resource shapes described here may change before release.

:::

Hermes Enterprise is built fail-closed: every boundary defaults to deny, and anything that cannot be positively verified does not run. This page maps the trust boundaries and states plainly what the system guarantees — and what it deliberately does not attempt in v1.

## Trust boundaries

| Boundary | Trusts | Is trusted for | Never trusted for |
|---|---|---|---|
| **Identity provider (OIDC)** | — (external root of trust) | Authenticating who a caller is (issuer, audience, claims) | Authorization decisions — the IdP says *who*, never *what they may do* |
| **Access gateway** | The configured OIDC issuer only | Token verification and tenant admission; rejecting everything unmappable | Resource-level authorization; it admits to a tenant, it does not grant actions |
| **Controller** | Identities admitted by the access gateway | Authorization (roles, bindings, restrictions), the resource store, deploy choreography, audit emission | Holding secret values; executing tenant workloads itself |
| **Drivers (compute / sandbox / secret)** | Controller instructions only | Their single capability: provisioning workloads, enforcing+verifying containment, performing secret operations | Making policy decisions; a driver executes, the controller decides |
| **Kubernetes** | Cluster admin configuration | Namespace isolation primitives, workload scheduling, network policy enforcement | Tenant admission or identity — a workload's K8s service account is not a Hermes identity |

Each layer only narrows what the layer above allowed. A compromised lower layer cannot mint identity or grants that the layers above never issued.

## Secret brokering

Secret values **never enter agent workloads**. The model:

1. A `Secret` resource is a *reference* — backend + key — not a value. The control plane store never contains secret material.
2. A `SecretBroker` in the tenant namespace is the only component that talks to the secret backend.
3. A workload, authenticated by its `WorkloadIdentity`, asks the broker to **use** a secret for a named operation (inject a credential at the egress proxy, sign a request, perform an exchange). The broker performs the operation and returns the *result*, never the value.
4. Whether a secret exists can be checked; what it contains cannot be read through the control plane at all.

Consequences: a fully compromised agent workload can abuse the operations its bindings allow (visible in audit), but it cannot exfiltrate the underlying credentials, and rotation happens in the backend without touching workloads.

## Sandbox verification

Enforcement and verification are separate steps, performed by the sandbox driver during deploy:

- **Enforce** applies the `SandboxPolicy` to the candidate workload.
- **Verify** independently checks the live workload against the policy afterward.

A candidate that fails verification is torn down and never serves — the route to it is not enabled. Verification runs on every deploy, including rollbacks to previously verified revisions. See [Deployment Choreography](./deployment.md).

## Audit guarantees

The controller emits a structured audit event for every control-plane action and every brokered secret operation. Guarantees:

- **No secret values in audit.** Events record *that* a secret was used, by which workload identity, for which operation — never the material.
- **No message contents in audit.** Conversations between users and agents are not part of the audit stream. Audit covers control-plane and broker activity, not chat transcripts.
- Events record the acting identity, the action, the target resource, and the outcome — including denials, so fail-closed rejections are observable.

## What Hermes Enterprise does NOT do in v1

Stated explicitly so nobody designs around a capability that is not there:

- **No cross-namespace references.** A resource in one namespace can never reference a resource in another. There is no sharing mechanism, no "global" configuration a tenant can import.
- **No external IAM adapters.** Identity comes from OIDC verification at the access gateway; authorization is the built-in Role/AccessBinding/Restriction model. There are no plug-in adapters for external policy engines or enterprise IAM suites in v1.
- **No non-Kubernetes compute.** The compute driver targets Kubernetes namespaces only. No VM fleets, no serverless backends, no bare-metal scheduling.

These are scope boundaries, not oversights — the contracts (driver registry, IAM interfaces) are designed so later versions can widen them without changing the trust model above.
