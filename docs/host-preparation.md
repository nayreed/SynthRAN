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

Task 1 of issue #124 declares this policy but does not implement the reconciliation engine. Until Task 4 lands, a resolved scenario may name `bootstrap`, but execution fails closed before any host-preparation command is attempted.

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
3. Task 4 implements the bounded `bootstrap` reconciler.
4. Task 5 defines live boot-state comparison and minimum reboot behavior.
5. Later tasks expose the mode in the interactive wizard, optimize fresh preparation, and physically validate the result.

This ordering prevents a partially implemented `bootstrap` mode from mutating physical hosts.
