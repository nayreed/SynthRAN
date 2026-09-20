# N2/N3/N4 transport contract

Issue #54 makes `deployment/topology.yml` the static authority for SynthRAN's N2/N3/N4 transport. The selected `rans.<ran>.<core>` entry is copied into `synthran_topology`, included in the deployment fingerprint, and consumed by host transport roles and chart adapters. A transport change therefore changes deployment identity and cannot be silently reused.

The pinned behavioral comparison source for this rework is the original `sopnode/5g_ansible` repository at `b73fccf87f55060484b3759e9cb347222253534b`. The repository-wide execution contract uses selective delegation rather than a fork-only machine API or full upstream playbook execution. Transport follows the reviewed upstream GRE/OVS and packet-path behavior where that behavior matches the selected chart contracts; SynthRAN retains the topology adapter where the generic upstream addresses or role ownership do not match the selected deployment.

`bridge_enabled` is a placement-derived compatibility value, not an independent topology knob: it is `true` exactly when the selected core and RAN hosts differ and `false` when they are colocated. Pure upstream has no `fiveg_machine.py` normalizer; SynthRAN derives this value from its selected node placement and validates the resulting packet-path contract directly. A scenario may state the matching value for compatibility, but it cannot override the derived transport mode. The authoritative transport reconciler itself always runs; placement determines whether its selected contract contains GRE links.

## Effective transport table

| Core profile | Placement | Host bridges | Host addresses / link | Workload-facing N2/N3 | Why |
| --- | --- | --- | --- | --- | --- |
| Open5GS | colocated | core: `n2br,n3br,n4br` | `n2br=10.10.2.254/24`, `n3br=10.10.3.254/24`, `n4br=10.10.4.254/24`; no GRE | core NADs map N2→`n2br`, N3→`n3br`, N4→`n4br`; RAN compatibility path uses `n3br` | Pinned Open5GS NADs require the three core bridges. The pinned srsRAN/Open5GS chart uses AMF `10.10.3.200` and gNB `10.10.3.234`, so the generic reference `192.168.3.x` GRE block is not the chart-compatible authority. |
| Open5GS | split | core: `n2br,n3br,n4br`; RAN: `n3br` | `n3br core=10.10.3.254/24`, `ran=10.10.3.253/24`; one unkeyed `gre-n3br` | AMF N2 endpoint `10.10.3.200`; RAN N2/N3 share `n3br` compatibility transport | Only N3 bridge spans hosts. N2/N4 remain core-local Open5GS attachments. |
| OAI | colocated | compatibility `n3br` exists | no host transport address or GRE | OAI/srsRAN colocated paths use the physical interface; the retained OAI+UERANSIM compatibility path selects `n3br` | No inter-host link is manufactured when there is no split. UERANSIM's retained OAI adapter is the explicit colocated OVS exception. |
| OAI | split | core: `n3br`; RAN: `n3br` | `n3br core=192.168.3.254/24`, `ran=192.168.3.253/24`; one unkeyed `gre-n3br` | AMF N2 endpoint `192.168.3.201`; OAI/srsRAN/UERANSIM adapters consume the selected N3 compatibility interface | This is the pinned generic OAI-compatible upstream path. |
| Free5GC | colocated | compatibility bridges remain available; no host transport address/GRE is assigned | none | chart adapters use the physical NIC/ipvlan path when core and RAN are colocated | The retained chart adapter does not require an inter-host OVS tunnel in the colocated case. Physical acceptance is still required before claiming this placement as proven. |
| Free5GC | split | core: `n3br,n4br`; RAN: `n3br,n4br` | `n3br core=10.100.50.238/29`, `ran=10.100.50.237/29`; one unkeyed `gre-n3br`; local `n3br↔n4br` patch on each SOP host | core logical N2/N3 use `n3br`; AMF N2=`10.100.50.234`; branching-UPF N3=`10.100.50.233`; N4 stays on the physical NIC in the pinned chart | Preserves the reviewed upstream Free5GC GRE/PATCH behavior. `n4br` is not a second inter-host GRE tunnel. |

The Free5GC RAN pod addresses are intentionally not forced to the host bridge prefix width. For example the pinned srsRAN Helm chart assigns its gNB attachment using its chart address while the host bridge uses the `/29` inter-host transport. That is a chart/attachment contract, not evidence that the host transport should be rewritten.

## Ownership and rendering

The source-to-render flow is:

```text
scenario deployment.topology_file + selected core/RAN nodes
        ↓ resolve and validate before reservation
deployment/topology.yml + placement-derived bridge_enabled
        ↓ selected rans.<ran>.<core>
synthran_topology + deployment fingerprint
        ├─ setup/ovs → required bridge names
        ├─ setup/gre_tunnel → colocated/split host addresses, GRE peers, Free5GC patch
        ├─ Open5GS → verifies topology-owned bridges + AMF endpoint
        ├─ Free5GC → N2/N3 master interfaces + AMF/UPF endpoints
        ├─ OAI helper → physical vs selected compatibility interface from placement
        ├─ srsRAN → selected network endpoints and N3 attachment
        └─ UERANSIM → selected N3 attachment/endpoints
```

`setup/cni` owns only the Kubernetes network-addons layer; it never creates host OVS bridges. CNAO exposes OVS-CNI independently of `bridge_enabled`, because OVS attachment capability is not the same thing as needing an inter-host GRE tunnel: colocated Open5GS uses pinned OVS NADs, and OAI+UERANSIM retains a colocated OVS compatibility attachment. Unused OVS-CNI availability does not choose a packet path; the selected topology and chart adapter do.

Workload roles must not assign host bridge addresses. `setup/gre_tunnel` is the single host-transport reconciler despite its historical name: it runs for colocated and split placements, assigns only the selected placement's host addresses, reconciles only SynthRAN-managed GRE/patch ports described by the selected contract, and deliberately uses unkeyed GRE to match the reviewed upstream behavior.

## Acceptance evidence

Every deployment imports `deployment/playbooks/attest_transport.yml` immediately after `network.yml`. It writes `results/<run>/transport-evidence.json` with:

- exact SynthRAN commit and deployment hash;
- the selected topology snapshot;
- `ovs-vsctl show` for each SOP node;
- `ip -j address show` and `ip -j route show table all` for each SOP node;
- all Kubernetes NetworkAttachmentDefinitions;
- pod placement/address summary.

Static checks and CI can prove that one configuration owner feeds the renderers, but they cannot prove the physical packet path. Issue #54 is physically complete only after an R2Lab run on the final SHA demonstrates the selected AMF/gNB endpoints and source-bound UE→N6 connectivity, with the generated `transport-evidence.json` attached or referenced.
