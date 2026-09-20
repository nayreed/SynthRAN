# Resource acquisition and POS preparation

SynthRAN is the single authority for external testbed acquisition and POS host preparation. This boundary is deliberately retained even as downstream host, Kubernetes, transport, core and RAN provisioning selectively delegates reviewed task files from the pinned original `sopnode/5g_ansible` execution reference.

The resolved deployment snapshot must contain explicit, independent policy for provider context, POS calendar acquisition, host preparation and R2Lab acquisition **before any remote mutation occurs**. `synthran.scenario` canonicalizes older `enabled` booleans into these fields when loading a source scenario; the private resolved scenario written by `deploy.sh` is therefore explicit before `synthran.reservation` runs. Interactive deployment exposes the same explicit choices in the wizard rather than deciding policy later inside the reservation engine.

## Policy schema

```yaml
deployment:
  nodes:
    core: sopnode-f2
    ran: sopnode-f3
    broker: sopnode-f2

  provider:
    mode: require-existing       # create | require-existing | disabled
    project: post5g-beta
    experiment: synthran
    experiment_duration: 4h     # used only when create must create it

  reservation:
    mode: require-existing       # create | require-existing | disabled
    host_preparation: preserve   # preserve | bootstrap | fresh
    duration_minutes: 120
    image: ubuntu-jammy

  r2lab_reservation:
    mode: require-existing       # book | require-existing | disabled
    duration_minutes: 120
```

These choices are separate on purpose. Reusing a calendar or R2Lab lease never implies permission to reimage a host. Likewise, preserving host state does not weaken exact-resource coverage verification.

`reservation.mode=disabled` requires `host_preparation=preserve`. Both `bootstrap` and `fresh` are mutating policies and therefore require proven POS calendar authority.

Legacy source scenarios are deterministic rather than interactive:

- POS `enabled: true` becomes `mode: create` and defaults host preparation to `fresh`.
- POS `enabled: false` becomes `mode: disabled` and defaults host preparation to `preserve`.
- R2Lab `enabled: true` becomes `mode: book`; `enabled: false` becomes `disabled`.
- A legacy `reservation.node_pool` is discarded because reservation handling is no longer allowed to substitute nodes.

New or edited scenarios should use the explicit modes directly.

## Exact-resource rule

The `deployment.nodes` role mapping is immutable inside reservation handling. Core, RAN and broker are deduplicated only to form the resource set sent to POS; the role mapping itself is preserved in evidence. There is no automatic substitution, manual replacement loop, `--asap` fallback or hidden calendar replacement.

For `require-existing`, SynthRAN requires exactly one caller-owned active POS calendar event whose node set exactly equals the selected resource set and whose end covers the requested duration. Missing or ambiguous coverage fails closed.

For `create`, an active exact caller-owned reservation may be reused when the event itself was originally booked for at least the configured duration. This permits a rerun inside the same booking without trying to create a second overlapping event. If no such active event exists, SynthRAN creates a reservation for exactly the selected nodes and verifies the returned event ID against provider-backed calendar evidence. It does not delete unrelated or earlier calendar events to make room for the request.

## Provider context

When provider mode is enabled, the order is:

1. select the requested SLICES project;
2. show the requested experiment;
3. when policy is `create`, create it only if missing;
4. query `post5g experiment prefix` with bounded retry;
5. require a JSON object containing non-empty `subnet`, `lb` and `expiration_time`.

`require-existing` never creates an experiment. Provider command errors remain visible in the failure evidence.

## POS host preparation

The canonical preparation contract is documented in [`host-preparation.md`](host-preparation.md) and implemented in `synthran/host_preparation.py`.

`preserve` is strictly verification-only and performs zero allocation, image, boot-parameter, package/service repair, reboot, reset, or other host mutation.

`bootstrap` is the bounded in-place reconciliation policy: it retains the current allocation and OS, forbids image staging/allocation reclaim/clean-image POS reset, and reconciles only the declared host/Kubernetes prerequisites after classifying the live state. A healthy existing cluster is reused; a clusterless host may rebuild Kubernetes in place; an existing but unhealthy cluster is classified `requires-fresh` and blocked. Safe boot drift on a clusterless POS live host may be corrected with the pinned boot parameters plus an ordinary reboot. Bootstrap never silently escalates to `fresh`. Classification and boot-reconciliation evidence are retained before R2Lab hardware mutation.

`fresh` requires proven calendar authority first and uses two phases. Phase 1 proves allocation authority for **every selected SOP node before any image/reset mutation occurs**. If one selected node cannot be allocated, no selected node has been reimaged. An already-active allocation may be reclaimed only after all selected nodes have first been probed and only under the explicit `fresh` policy.

After allocation authority is proven for the complete selected resource set, phase 2 prepares independent selected SOP nodes concurrently. The per-node sequence remains strict:

1. select the resolved scenario image with the reviewed upstream staging mechanic;
2. apply the pinned original-upstream SOP/N3xx boot parameters;
3. perform a blocking POS reset;
4. prove SSH readiness with a bounded retry.

Calendar acquisition, allocation probing and any required allocation reclaim remain serialized authority work. If one parallel node fails, SynthRAN cancels work that has not started and signals already-running peers to stop at the next safe per-node phase boundary. Any provider command already in flight is allowed to settle rather than being orphaned. Per-node completion/cancellation/failure state is retained and the preparation fails as a whole.

The user-facing `ubuntu-jammy` alias resolves before POS mutation to the pinned provider artifact `ubuntu-jammy-slices@2025-04-02T01:33:28+00:00`; explicit full image identifiers remain unchanged. The resolved private scenario and evidence therefore record the actual provider image.

Fresh preparation prints progress at allocation probing/reclaim, image staging, boot parameters, blocking reset and SSH readiness so a long POS operation is not presented as a silent deployment hang.

## R2Lab authority

The R2Lab helper retains SynthRAN's credential and evidence semantics. Password material is read from stdin only when a `book` policy actually needs a booking or extension; it is never inserted into shared argv or logs. SSH identity-file and known-hosts support remain explicit.

- `require-existing`: exactly one owned lease must already cover the complete requested interval. No booking, extension or password read is allowed.
- `book`: an exact covering lease is reused; one compatible owned short lease may be extended and then verified by ID; otherwise a new lease may be booked and must be re-queried as exactly one covering lease.
- `disabled`: no provider access occurs.

Multiple covering leases or multiple owned overlapping leases fail closed rather than guessing which lease is authoritative.

### Faraday SSH handoff

Provider lease verification and downstream Ansible provisioning must use the same R2Lab gateway identity and connection policy. For `faraday.inria.fr`, SynthRAN deliberately supplies `-F /dev/null` so a Duckburg/controller-local `~/.ssh/config` cannot silently inject a different proxy or port. The connection is non-interactive, uses the selected known-hosts file, `StrictHostKeyChecking=accept-new`, a bounded connect timeout, and `IdentitiesOnly=yes` when an explicit R2Lab identity file is configured.

The physical-UE Ansible inventory uses Faraday as a `ProxyCommand` with that same gateway policy. The outer UE SSH identity behavior is preserved; only the gateway hop is insulated from unrelated controller SSH configuration.

R2Lab cleanup is selected-resource scoped. It stops only the UEs present in the resolved inventory and powers off only the selected `rru`. The obsolete global `all-off` mutation is forbidden because it can affect resources outside the current deployment.

## Evidence

Every reservation pass writes `results/<run>/reservation-authority.json` incrementally. It records the selected role/resource identity, explicit policies, provider context, POS calendar ID/coverage and host-preparation evidence. Multi-node fresh preparation retains per-node phase/status evidence, including partial failure state when one independently prepared node fails. Bootstrap additionally retains SOP preflight, repairability classification, and (when needed) boot-reconciliation evidence before physical R2Lab mutation. If a later mutation fails, earlier known ownership identifiers remain in the run evidence together with the original error message.

The accepted deployment identity also retains the canonical `reservation_mode` and `host_preparation` values; `pos_image` is bound only for the `fresh` path so preserve/bootstrap do not claim an unused image. Host runtime provenance records the preparation mode observed for the run.

`pos-selection.json` remains as a compatibility/evidence surface for the deployment runner, including POS coverage end used to constrain an R2Lab lease window. It no longer carries remapped node identities because reservation handling cannot remap them.

## Downstream 5g-Ansible boundary

The pinned execution/comparison authority is the original `sopnode/5g_ansible@b73fccf87f55060484b3759e9cb347222253534b`. Pure upstream does **not** provide the fork-only provider/reservation machine API or a `pos_manage_allocation=false` suppression switch. Its POS role frees and allocates before the `no_boot` guard, and `playbooks/deploy_r2lab.yml` also owns cleanup, RRU and UE preparation.

Therefore, after SynthRAN has acquired or prepared external resources, these full upstream entrypoints are forbidden:

```text
playbooks/deploy.yml
playbooks/deploy_r2lab.yml
playbooks/run_pos.yml
```

Downstream delegation is limited to task files explicitly reviewed in `third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json`. `tools/check_resource_authority_contract.py` proves both sides of this boundary: the original upstream still owns/reacquires external state through its full entrypoints, and SynthRAN runtime code does not invoke those entrypoints from the pinned checkout. This prevents a second provider, calendar, R2Lab, allocation or boot engine from becoming active later in the call graph.

## Validation boundary

The reservation contract checks use fake command responses and no physical testbed. They cover provider create/existing/retry/failure, exact POS create/require-existing/unavailable/ambiguous coverage, active-calendar rerun reuse, resolved POS image identifiers, two-phase multi-node fresh ordering, guarded allocation conflict recovery, preserve zero-mutation behavior, explicit wizard policy selection, R2Lab reuse/extension/booking failure/ambiguity/disabled behavior, password stdin-only transport, the Faraday Ansible/ProxyCommand SSH contract, selected-resource cleanup, stdin-EOF safety and selected-node immutability.

Passing these checks proves the local contract only. Physical correctness and experiment eligibility belong to the later integrated acceptance issue.
