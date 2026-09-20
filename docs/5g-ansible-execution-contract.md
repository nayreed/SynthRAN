# Pinned 5G-Ansible execution contract

SynthRAN uses the **original upstream** repository `sopnode/5g_ansible` at commit
`b73fccf87f55060484b3759e9cb347222253534b` as its immutable deployment-behavior
reference.

This execution/comparison pin is deliberately separate from historical derivation
provenance in `third_party/sopnode-5g-ansible/SOURCE.json`. `SOURCE.json` records
where the retained deployment code originally came from; it is not the live
execution authority.

The machine-readable execution contract is
`third_party/sopnode-5g-ansible/EXECUTION_REFERENCE.json`.

## Why this is selective delegation

Pure `sopnode/5g_ansible` does **not** contain the fork-added `bin/fiveg` /
`tools/fiveg_machine.py` interface. SynthRAN therefore validates and delegates
reviewed upstream task files directly rather than depending on a fork-specific
controller API.

The complete upstream playbooks are not safe to call after SynthRAN has acquired
resources:

- `playbooks/deploy_r2lab.yml` owns R2Lab cleanup, RRU power and UE preparation;
- `playbooks/deploy.yml` invokes POS preparation for the selected SOP nodes;
- the upstream POS role performs `pos allocations free` and
  `pos allocations allocate` before the `no_boot`-guarded image/reset section.

There is no upstream equivalent of the fork-only `pos_manage_allocation=false`
suppression surface. Therefore these complete upstream entrypoints are forbidden
across SynthRAN's resource-authority boundary:

```text
playbooks/deploy_r2lab.yml
playbooks/deploy.yml
playbooks/run_pos.yml
```

SynthRAN instead delegates only task files that have been explicitly reviewed by
the dependent sub-issue.

## Ownership rule

There must be one mutation owner for every responsibility.

**SynthRAN owns:**

- SLICES project/experiment context;
- POS calendar, allocation, image, boot-parameter, reset and post-reset readiness;
- R2Lab lease acquisition/extension/require-existing evidence;
- SSH identity-file handling;
- source/scenario translation and validation;
- scientific deployment identity and evidence;
- final physical acceptance and experiment eligibility.

**Original upstream may own, only where explicitly reviewed:**

- selected host/Kubernetes lifecycle task files;
- selected OAI core lifecycle task files;
- selected srsRAN startup/RFSIM task files;
- other downstream task files only after the relevant sub-issue proves the
  boundary.

A delegated upstream task completing successfully is **not** equivalent to a
SynthRAN deployment being accepted or experiment-eligible.

## Current source-to-reference mapping

| SynthRAN source | Original-upstream surface | Contract |
| --- | --- | --- |
| `deployment.core` | `core` / core role selection | Direct after validation. |
| `deployment.ran` | `ran` / RAN role selection | `srsran` adapts to upstream `srsRAN`; OAI/UERANSIM retain upstream names. |
| `deployment.platform` | platform/R2Lab role selection | Direct conceptually; SynthRAN does not delegate the full R2Lab preparation playbook. |
| `deployment.ru` | `rru` | Direct for reviewed physical paths. |
| core/RAN node selection | inventory groups + `*_node_name` | SynthRAN renders the selected inventory explicitly. |
| physical UE selection | `qhats` / `qfits` inventory groups | Adapted; resource selection, subscriber identity and modem attachment remain distinct. |
| effective network profile | upstream `fiveg_profile` / role `include_vars` | Backend-specific adapter required; rendered parity is validated rather than assumed. |
| `R2LAB_IDENTITY_FILE` | no direct upstream equivalent | SynthRAN-owned. |
| accepted deployment | no upstream equivalent | SynthRAN-owned evidence decision. |

## Reviewed delegated task files

The current exact allow-list lives in `EXECUTION_REFERENCE.json`. It includes the
reviewed host/Kubernetes forwarding tasks plus selected OAI-core and srsRAN task
files. Runtime code must not resolve or launch the forbidden complete upstream
entrypoints through the pinned checkout.

The shared materializer is:

```bash
python -m synthran.reference_checkout
```

It reads the repository and SHA from `EXECUTION_REFERENCE.json`, fetches that
exact revision, verifies `HEAD`, rejects local modifications and returns the
immutable checkout path.

For contract validation use:

```bash
python tools/check_5g_ansible_contract.py --reference /path/to/sopnode-5g_ansible
python tools/check_resource_authority_contract.py --reference /path/to/sopnode-5g_ansible
python tools/check_host_bootstrap_contract.py --reference /path/to/sopnode-5g_ansible
```

A failure means the pinned upstream source or a SynthRAN ownership assumption no
longer matches the reviewed contract. Re-audit the boundary instead of adding a
fallback implementation.

## Important differences from the superseded fork-based contract

The former reference `nayreed/5g-Ansible@6c9cb3...` was a fork-derived merge tree
that was ahead of pure upstream and introduced behavior not present in
`sopnode/5g_ansible`, including the declarative `fiveg` machine interface and a
POS-allocation suppression surface. Those fork-only behaviors are not part of the
current authority contract.

This changes several earlier assumptions:

- node/UE/profile behavior is now checked directly against upstream inventory,
  profiles, roles and playbooks rather than a machine normalizer;
- full-playbook delegation is disallowed instead of relying on a synthetic
  resource-suppression overlay;
- srsRAN's pure-upstream physical retry task is used as the startup owner, while
  SynthRAN's separate RAN-health verifier remains the acceptance owner;
- physical revalidation is required where the pure-upstream task differs from
  the former fork-derived task.

## Current dependent gaps

- **Sub 06 / #55 — OAI + N320:** implementation remains path-specific and still
  needs physical OAI/N320 acceptance evidence.
- **Sub 07 / #60 — srsRAN:** pure-upstream `deploy_with_check.yml` differs from
  the former fork-derived task. Local RF/PHY/N2 acceptance remains authoritative;
  a fresh physical srsRAN/N320 run is required.
- **Sub 09 / #56 — physical UE lifecycle:** upstream `test-ue-connect.yml` is a
  separate playbook and ignores individual connection failures. SynthRAN must
  retain explicit selected-UE identity, fail-closed mutation and read-only
  verification.
- **Sub 10 / #57 — acceptance:** delegated provisioning completion must never
  promote stale or incomplete state to experiment eligibility.

## Deletion rule

When a reviewed original-upstream task file owns a responsibility completely,
remove the parallel local implementation. Keep only the adapter/evidence logic
that SynthRAN genuinely requires. Do not maintain a fork engine, an original-
upstream engine and a local engine as selectable fallbacks.
