# SOP host-preparation contract

SynthRAN treats host preparation as an explicit deployment policy. The policy is resolved before host mutation and is independent from POS calendar acquisition or R2Lab reservation.

The canonical values are `preserve`, `bootstrap`, and `fresh`.

## preserve

Use `preserve` only when the selected SOP nodes are already in the exact host/Kubernetes state required by the deployment.

Permitted behavior:

- inspect and verify the current host state;
- retain evidence describing what was observed.

Forbidden behavior:

- allocation reclaim;
- POS image staging;
- boot-parameter mutation;
- package installation or host-service repair;
- Kubernetes/CNI/OVS repair;
- reboot or POS reset;
- silent fallback to another preparation mode.

A missing prerequisite is therefore a hard failure. `preserve` means verification-only; it does not mean "reuse what exists and fix the rest."

## bootstrap

`bootstrap` is the bounded in-place reconciliation policy.

Its contract is:

- keep the current POS allocation;
- keep the current OS installation;
- require POS calendar authority before mutating the selected host;
- idempotently establish missing SynthRAN host prerequisites;
- reuse prerequisites that already satisfy the contract;
- allow only explicit, evidence-backed boot-parameter changes and a reboot when that is sufficient;
- never stage a POS image;
- never reclaim the allocation merely to establish prerequisites;
- never issue the destructive POS reset used by the known-clean fresh path;
- never silently escalate to `fresh`;
- fail and instruct the operator to choose `fresh` when the current host cannot be reconciled safely.

Bootstrap is executable only after an evidence-backed classification of the current host and cluster state. The classifier records one of `healthy`, `repairable-in-place`, `reboot-required`, or `requires-fresh` before R2Lab hardware mutation.

For an existing healthy Kubernetes identity, bootstrap reuses the cluster and reconciles only declared prerequisites. A clusterless host may rebuild Kubernetes in place while retaining the current POS allocation and OS. An existing but unhealthy Kubernetes identity is not reset speculatively: it is classified `requires-fresh` and the deployment stops with an explicit instruction to select `fresh`.

Boot-profile drift is corrected automatically only when the state is unambiguous and safe on the supported Jammy POS live baseline. The required boot parameters are applied without image staging, affected SOP hosts reboot sequentially with an ordinary OS reboot, and a reused Kubernetes cluster must recover its control plane and selected RAN node before any R2Lab mutation begins. Bootstrap never uses POS image staging or the clean-image POS reset path. If the corrected profile does not become active, the reused cluster does not recover, the OS/release is unsupported, or an existing Kubernetes identity is unhealthy, bootstrap stops and requires `fresh` rather than escalating silently.

## fresh

`fresh` is the known-clean preparation path and remains the authoritative fallback when current host state is unknown, incompatible, or not safely repairable in place.

Permitted behavior includes:

- proving allocation authority;
- reclaiming an already-active selected allocation under the explicit fresh policy;
- staging the selected POS image;
- applying the pinned boot parameters;
- issuing the blocking POS reset;
- waiting for SSH readiness;
- installing/configuring the complete host, Kubernetes, CNI, OVS, and related prerequisite stack.

`fresh` must not be entered implicitly from another mode.

## Operator decision guide

Choose the least-mutating mode justified by what you know about the selected SOP hosts:

```text
Current host + Kubernetes state already proven complete?
├─ yes -> preserve
│          verification only; any missing prerequisite is a hard failure
└─ no
   ├─ valid retained Ubuntu/Jammy worth keeping and safe to reconcile?
   │  ├─ yes -> bootstrap
   │  │          classify first; repair only supported prerequisites;
   │  │          bounded reboot only when explicitly justified
   │  └─ no  -> fresh
   │             known-clean image/reset baseline
   └─ bootstrap says requires-fresh -> stop and choose fresh explicitly
```

- Use **preserve** after a previous accepted deployment or when independent evidence says the complete prerequisite stack is still healthy. It is verification-only.
- Use **bootstrap** for a valid retained Ubuntu/Jammy host that may be missing Kubernetes, CNI, OVS, sysctl, or related SynthRAN prerequisites.
- Use **fresh** when host history is uncertain, an existing Kubernetes identity is unhealthy, the OS/release is unsupported, boot state cannot be reconciled safely, or a deterministic clean baseline is required.

Do not choose `preserve` merely because you want a faster deployment. `preserve` is a verification policy, not an optimization flag.

### Bootstrap fallback rule

Bootstrap never performs an automatic fresh image/reset fallback. When classification reports `requires-fresh`:

1. the current deployment stops before physical R2Lab mutation;
2. inspect `sop-bootstrap-classification-<host>.json` and the failure message;
3. keep the failed run as evidence;
4. start a new deployment and choose `fresh` explicitly.

### Evidence to inspect

Under `results/<run-id>/`:

- `reservation-authority.json` — selected policy and external-resource authority;
- `sop-preflight-<host>.json` — observed host prerequisite state;
- `sop-bootstrap-classification-<host>.json` — bootstrap decision and reasons;
- `sop-bootstrap-boot-<host>.json` — bounded boot correction/reboot evidence when used;
- `bootstrap-evidence.json` — final host/Kubernetes bootstrap state;
- `phase-timings.json` — reservation/preparation/deployment/verification durations;
- `deployment-fingerprint.json` — final deployment identity;
- `live-deployment-evidence.json` — UE/session/user-plane acceptance evidence.

For the issue #124 physical acceptance procedure, see [`issue124-r2lab-validation.md`](issue124-r2lab-validation.md).

## Authority matrix

| Capability | preserve | bootstrap | fresh |
| --- | --- | --- | --- |
| Read/verify host state | yes | yes | yes |
| Requires POS calendar authority for host mutation | no mutation | yes | yes |
| Reconcile packages/services/Kubernetes prerequisites | no | yes | yes |
| Reclaim allocation | no | no | yes |
| Stage POS image | no | no | yes |
| Change boot parameters | no | bounded/explicit | yes |
| Reboot host | no | bounded/explicit | yes |
| POS reset used by clean-image path | no | no | yes |
| Silent escalation to fresh | no | no | no |

The implementation source of truth for this vocabulary and matrix is `synthran/host_preparation.py`.

## Rollout boundary

Issue #124 is intentionally incremental:

1. Task 1 defines and validates this three-mode contract.
2. Tasks 2-3 move viability checks before physical R2Lab mutation.
3. Tasks 4-6 implement bounded reconciliation, boot-state handling, and repairability classification.
4. Later tasks expose the mode in the interactive wizard, optimize fresh preparation, and physically validate the result.

The classifier and its evidence run before any R2Lab cleanup, RRU power cycle, or selected-UE preparation.
