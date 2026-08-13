---
sidebar_position: 4
title: "Installation"
description: "Install Hermes Enterprise on Kubernetes with Helm, configure OIDC trust, and deploy your first agent"
---

# Installation

:::warning Status: draft / under development

This section documents Hermes Enterprise while its components are still landing as in-flight pull requests. Interfaces, commands, and resource shapes described here may change before release.

:::

Hermes Enterprise installs onto an existing Kubernetes cluster with Helm. The chart lives at `deploy/enterprise/helm` and ships with the enterprise packaging.

## Prerequisites

- A Kubernetes cluster (1.28+) you have admin access to, with a working ingress controller
- Helm 3
- An OIDC identity provider (issuer URL + a client/audience for Hermes Enterprise)
- `kubectl` context pointing at the target cluster

## Helm quickstart

```bash
helm install hermes-enterprise ./deploy/enterprise/helm \
  --namespace hermes-system \
  --create-namespace \
  --values values.yaml
```

This installs the controller and the access gateway into `hermes-system`. Tenant namespaces are **not** created by the chart — they are created later through the control plane, which provisions the per-tenant Kubernetes namespace, gateway, and workloads itself.

## OIDC trust configuration

The access gateway admits a request only when it can verify the caller's token against a configured trust and map the identity to a tenant. Configure trust in your `values.yaml`:

```yaml
accessGateway:
  oidc:
    issuer: "https://login.example.com/realms/acme"
    audience: "hermes-enterprise"
    # Map verified identities to tenant namespaces.
    tenantMap:
      # by claim: members of these IdP groups land in these namespaces
      claim: "groups"
      rules:
        - match: "acme-research"
          namespace: "research"
        - match: "acme-support"
          namespace: "support"
```

Fail-closed rules apply: a token from any other issuer, with the wrong audience, or whose claims match no tenant rule is rejected at the gateway. There is no default tenant.

## First namespace and first agent

The enterprise CLI drives the control plane. Run it from the repo (or the packaged wheel) with a kube context that can reach the controller:

```bash
# 1. Initialize the control plane store and register your identity as the
#    initial administrator.
python -m enterprise.cli init

# 2. Create a tenant namespace. This provisions the Kubernetes namespace
#    and the per-tenant gateway.
python -m enterprise.cli ns create research

# 3. Register a Harness — the hermes-agent runtime version agents will run.
python -m enterprise.cli harness register hermes-agent:1.9.0

# 4. Put a Configuration into the namespace (models, tools, behavior).
python -m enterprise.cli config put research/default --file ./agent-config.yaml

# 5. Declare an Agent that ties the configuration and harness together.
python -m enterprise.cli agent create research/assistant \
  --config default \
  --harness hermes-agent:1.9.0

# 6. Deploy. This creates an immutable AgentRevision and walks it through
#    the deploy choreography.
python -m enterprise.cli agent deploy research/assistant
```

The deploy command reports each stage as it completes (candidate → containment verified → route prepared → previous retired → activate → route enabled). See [Deployment Choreography](./deployment.md) for what each stage means and how failures behave.

Once the route is enabled, users whose OIDC identity maps to the `research` tenant can reach the agent through the ingress.

## Verifying the install

- `kubectl get pods -n hermes-system` — controller and access gateway should be `Running`.
- `python -m enterprise.cli ns create <name>` failing with an authorization error means your identity has no AccessBinding — re-run `init` checks or ask an admin for a binding; this is the fail-closed model working as intended.
- Deploy stalls at *containment verified* mean the SandboxPolicy could not be verified against the candidate workload; the candidate is torn down and the previous revision (if any) keeps serving.

## Next steps

- [Concepts & Resource Model](./concepts.md) — what each resource kind does
- [Security Model](./security.md) — trust boundaries, secret brokering, audit
