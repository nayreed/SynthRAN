# Issue #124 physical R2Lab validation

This runbook closes the physical acceptance boundary for the SOP host-preparation refactor.

Static CI is necessary but insufficient. Task 13 requires two **real R2Lab deployment runs** produced by a clean source revision that contains the issue #124 implementation.

The canonical validator is:

```bash
python tools/validate_issue124_physical.py \
  --preserve-failure results/<negative-run> \
  --bootstrap-success results/<positive-run> \
  --expected-revision "$(git rev-parse HEAD)"
```

By default the positive run must contain `qhat01` and `qhat03`. Use repeated `--expected-ue` arguments if the authorized validation uses a different selected qhat set.

The validator writes `results/<positive-run>/issue124-physical-validation.json`. Task 13 should be checked only after that report says `"task13": "passed"`.

## Required topology

The positive bootstrap proof uses:

```text
Core:        OAI on sopnode-f2
RAN:         OAI on sopnode-f3
Platform:    R2Lab
Radio unit:  N320
Preparation: bootstrap
```

At least the selected physical qhats must reach verified modem identity, PDU/session state, and source-bound user-plane acceptance.

## Proof A — preserve fails before physical mutation

Purpose: prove that an invalid SOP host state is rejected before SynthRAN cycles the N320 or selected qhats. The negative proof may fail either at the initial SOP reachability/authentication gate or at the later detailed prerequisite preflight; both are part of the early host-viability boundary.

Use an authorized validation window in which the selected SOP hosts are genuinely incomplete for `preserve`—for example, a clean POS Ubuntu baseline that has not yet received the SynthRAN Kubernetes prerequisites.

**Do not remove kubelet, CNI, OVS, or other working prerequisites from a healthy shared host merely to manufacture this failure.**

Run the ordinary deployment controller and select OAI core, OAI RAN, R2Lab/N320, split SOP nodes (`sopnode-f2` core and `sopnode-f3` RAN), the validation qhats, and `preserve` host preparation.

Expected result:

- reservation authority may succeed;
- if SSH reachability succeeds, detailed SOP preflight evidence is written for both selected SOP nodes;
- preserve fails either because the selected SOP host is not reachable/authenticatable or because a required prerequisite is missing/unhealthy;
- controller exit code is non-zero;
- no `r2lab_rru_setup` timing exists;
- no `ue_setup` timing exists;
- `ansible.log` contains no selected-RRU power mutation and no qhat power/init/detach mutation.

Keep the complete result directory.

## Proof B — bootstrap reaches accepted testbed

From a retained, supported Ubuntu/Jammy SOP state that needs in-place preparation, run the same topology with `bootstrap` host preparation.

Expected behavior:

1. exact SOP/R2Lab authority is established;
2. SOP preflight and bootstrap classification complete before R2Lab mutation;
3. each SOP host is classified as `healthy`, `repairable-in-place`, or `reboot-required`;
4. bootstrap does **not** stage a POS image or issue the clean-image POS reset;
5. missing supported prerequisites are reconciled in place;
6. if boot drift is safely repairable, only the authorized bootparameter correction and ordinary reboot occur;
7. Kubernetes bootstrap/reuse completes;
8. only then does N320/qhat preparation begin;
9. OAI core + OAI RAN deploy;
10. physical UE/session/user-plane verification succeeds;
11. `deployment-fingerprint.json` reaches `accepted-testbed`;
12. controller exits 0.

The positive run's `phase-timings.json` must show successful:

```text
kubernetes_bootstrap
r2lab_rru_setup
ue_setup
stack_deployment
verification
```

It must not contain `pos_image_staging` or `pos_reset`; those are fresh-only operations.

## Validate the pair

Use the exact current source revision as the minimum accepted baseline:

```bash
cd ~/SynthRAN
git switch rework
git pull --ff-only

BASELINE="$(git rev-parse HEAD)"

python tools/validate_issue124_physical.py \
  --preserve-failure results/<negative-run> \
  --bootstrap-success results/<positive-run> \
  --expected-revision "$BASELINE"
```

For a different qhat set:

```bash
python tools/validate_issue124_physical.py \
  --preserve-failure results/<negative-run> \
  --bootstrap-success results/<positive-run> \
  --expected-revision "$BASELINE" \
  --expected-ue qhat01 \
  --expected-ue qhat23
```

The checker rejects a dirty-worktree run, a run older than the requested baseline, the wrong split-node topology, the wrong core/RAN/platform/RU, missing acceptance evidence, unverified physical modems, failed user plane, physical mutation in the negative proof, or fresh-only POS phases in the bootstrap proof.

## What to attach to issue #124

Keep the full run directories locally. The concise issue evidence is:

- negative run ID and source revision;
- positive run ID and source revision;
- `issue124-physical-validation.json`;
- the positive run's `phase-timings.json`;
- a short note identifying any bootstrap classifications (`healthy`, `repairable-in-place`, `reboot-required`).

Do not close Task 13 based only on CI or a pre-refactor deployment.
