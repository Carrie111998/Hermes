---
sidebar_position: 2
title: "Concepts & Resource Model"
description: "Hermes Enterprise resource kinds, IAM entities, and the fail-closed philosophy"
---

# Concepts & Resource Model

:::warning Status: draft / under development

This section documents Hermes Enterprise while its components are still landing as in-flight pull requests. Interfaces, commands, and resource shapes described here may change before release.

:::

Hermes Enterprise is driven by a declarative resource model. You declare resources; the controller reconciles them into running workloads. Every resource is an envelope of **metadata + kind-specific spec + controller-owned status**, and names must be lowercase DNS-1123 labels.

Resources live in one of two scopes:

- **Installation scope** — global to the control plane (Namespace, Harness).
- **Namespace scope** — owned by a single tenant (everything else).

Cross-namespace references are rejected. A resource in `tenant-a` can never point at a resource in `tenant-b`.

## Resource kinds

| Kind | Scope | What it is |
|---|---|---|
| **Namespace** | Installation | A tenant boundary. Maps 1:1 to a Kubernetes namespace holding that tenant's gateway and agent workloads. All namespace-scoped resources live inside exactly one. |
| **Configuration** | Namespace | A named bundle of agent configuration (models, tools, behavior settings) that agents reference. Versioned by the store; deploys snapshot it. |
| **Agent** | Namespace | The declared intent for a running agent: which Configuration, Harness, Channels, and policies it uses. Mutable — editing an Agent does not change anything running until a deploy creates a new revision. |
| **AgentRevision** | Namespace | An **immutable snapshot** of an Agent and everything it referenced at deploy time. What actually runs. Revisions are never edited; rolling forward or back means activating a different revision. |
| **Harness** | Installation | A registered hermes-agent runtime image/version that revisions pin to. Registering a harness makes a runtime version available; it grants nothing by itself. |
| **Channel** | Namespace | An ingress/egress binding for an agent (e.g. a messaging platform connection) attached to the namespace's gateway. |
| **Secret** | Namespace | A named reference to a value held in an external backend. The control plane stores the *reference*, never the value. |
| **SecretBroker** | Namespace | The broker configuration that performs operations against secret backends on a workload's behalf. **Secret values never enter agent workloads** — workloads ask the broker to *use* a secret (sign, inject at the proxy, exchange), and only the broker touches the backend. |
| **SandboxPolicy** | Namespace | Containment requirements for agent workloads (isolation level, filesystem/network constraints). A policy is **verified against the live workload before it is allowed to start serving** — an unverifiable policy means the workload does not start. |
| **Restriction** | Installation or Namespace | A constraint layered on top of existing grants, expressed as `<action>:<kind>[:<name>]` patterns. A Restriction **narrows what is already allowed — it never grants anything**. Installation-scoped restrictions bound every namespace; namespace-scoped ones bound only their tenant. |

## IAM entities

Identity and access are modeled separately from the resource kinds above:

| Entity | What it is |
|---|---|
| **Principal** | A human identity, established by OIDC verification at the access gateway. |
| **ServicePrincipal** | A non-human caller (CI systems, external automation) with its own credentials and bindings. |
| **WorkloadIdentity** | The identity a running agent workload holds. This is how a workload authenticates to the broker and controller — it is never a user's identity. |
| **Group** | A set of principals, typically mapped from the identity provider's claims. |
| **Role** | A named bundle of allowed actions on resource kinds. |
| **AccessBinding** | Attaches a Role to a Principal, ServicePrincipal, or Group within a scope. Nothing is permitted without a binding. |

## Fail-closed philosophy

Every decision point in the control plane defaults to **no**:

- A request with no verifiable identity is rejected at the access gateway — it never reaches the controller.
- An action with no matching AccessBinding is denied. There are no implicit or default grants.
- Restrictions can only subtract from that result, never add to it.
- A SandboxPolicy that cannot be verified against the running workload prevents the workload from serving.
- A dangling or cross-namespace reference fails validation at write time rather than being resolved leniently later.
- A driver (compute, sandbox, secret) that is not explicitly selected for a capability is an error, not a fallback.

The practical consequence: misconfiguration in Hermes Enterprise manifests as *things refusing to start or serve*, not as workloads quietly running with broader access than intended.
