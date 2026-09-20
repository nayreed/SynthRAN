#!/usr/bin/env python3
"""Executable contract for the shared #60 physical cross-RAN transition."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "deployment/playbooks/network.yml"
DEFAULTS = ROOT / "deployment/roles/5g/ran_transition/defaults/main.yml"
ROLE = ROOT / "deployment/roles/5g/ran_transition/tasks/main.yml"
OAI_RAN = ROOT / "deployment/roles/5g/oai/ran/tasks/main.yml"
SRSRAN_GNB = ROOT / "deployment/roles/5g/srsRAN/deploy/tasks/deploy_gnb.yml"


def require(value: bool, message: str) -> None:
    if not value:
        raise SystemExit(message)


def text(path: Path) -> str:
    require(path.is_file(), f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def static_contract() -> None:
    network = text(NETWORK)
    role = text(ROLE)
    defaults_text = text(DEFAULTS)
    catalog = yaml.safe_load(defaults_text)["synthran_ran_transition_catalog"]

    require(network.count("name: 5g/ran_transition") == 1, "transition owner must be singular")
    idx = network.index("name: 5g/ran_transition")
    require(idx > network.index("name: 5g/oai/core"), "transition must follow optional OAI core")
    require(idx < network.index("name: 5g/oai/ran"), "transition must precede OAI RAN")
    require(idx < network.index("name: 5g/srsRAN/config"), "transition must precede srsRAN")
    for gate in (
        "platform | string | trim | lower == 'r2lab'",
        "rru | string | trim | lower in ['n300', 'n320']",
        "ran | string | trim | lower in ['oai', 'srsran']",
    ):
        require(gate in network, f"missing physical transition gate: {gate}")

    require(
        [x["release"] for x in catalog["oai"]]
        == ["oai-gnb", "oai-du", "oai-cu", "oai-cu-up", "oai-cu-cp"],
        "OAI RAN catalog drifted",
    )
    require([x["release"] for x in catalog["srsran"]] == ["srsran-gnb"], "srsRAN catalog drifted")
    for forbidden in ("oai-flexric", "oai-nr-ue", "srsran-ue"):
        require(forbidden not in defaults_text, f"non-RAN owner leaked into transition: {forbidden}")

    for required in (
        "--all-namespaces",
        "spec.nodeName={{ ran_node_name }}",
        "Add an old namespace only when an exact incompatible pod is live on the selected RAN node",
        "Expand each proven stale namespace to the complete non-selected RAN family",
        "--cascade",
        "foreground",
        "--wait",
        "--no-hooks",
        "Remove a known orphaned incompatible Deployment",
        "Remove any known orphaned incompatible pods and wait for CNI teardown",
        "Recheck incompatible pods before releasing RU attachment definitions",
        "Refuse RU-NAD deletion while an incompatible pod remains",
        "Prove exclusive pre-launch ownership for the selected RAN",
        "ran-transition.json",
    ):
        require(required in role, f"transition lost required surface: {required}")

    require("helm, list, --all-namespaces" not in role, "Helm mutation discovery must not be global by name")
    for forbidden in ("kubeadm reset", "helm install", "helm upgrade", "192.168.3.203"):
        require(forbidden not in role, f"transition crossed ownership boundary: {forbidden}")
    require("srsran-gnb" not in text(OAI_RAN), "OAI backend owns cross-stack srsRAN cleanup")
    for name in ("oai-gnb", "oai-du", "oai-cu"):
        require(name not in text(SRSRAN_GNB), f"srsRAN backend owns cross-stack cleanup: {name}")


HELM=r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root=Path(os.environ["SYNTHRAN_RAN_TRANSITION_FIXTURE"])
def load(n): return json.loads((root/n).read_text())
def save(n,v): (root/n).write_text(json.dumps(v))
args=sys.argv[1:]
if args[:2]==["version","--short"]:
    print("v3.22.0+fixture"); raise SystemExit(0)
if args and args[0]=="status":
    name=args[1]; ns=args[args.index("--namespace")+1]
    items=[x for x in load("releases.json") if x["name"]==name and x["namespace"]==ns]
    if not items: raise SystemExit(1)
    print(json.dumps({"info":{"status":items[0]["status"]}})); raise SystemExit(0)
if args and args[0]=="uninstall":
    name=args[1]; ns=args[args.index("--namespace")+1]
    items=load("releases.json")
    live=[x for x in items if x["name"]==name and x["namespace"]==ns and x["status"]!="uninstalled"]
    if len(live)!=1: raise SystemExit(1)
    save("releases.json",[x for x in items if not (x["name"]==name and x["namespace"]==ns and x["status"]!="uninstalled")])
    with (root/"uninstalls.log").open("a") as f: f.write(" ".join(args)+"\n")
    print("uninstalled"); raise SystemExit(0)
raise SystemExit(2)
'''

KUBECTL=r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root=Path(os.environ["SYNTHRAN_RAN_TRANSITION_FIXTURE"]); args=sys.argv[1:]
def load(n): return json.loads((root/n).read_text())
def save(n,v): (root/n).write_text(json.dumps(v))
def ns():
    return args[args.index("--namespace")+1] if "--namespace" in args else None
def sel():
    if "--selector" not in args: return {}
    return dict(p.split("=",1) for p in args[args.index("--selector")+1].split(","))
def matches(x,w): return all((x["metadata"].get("labels") or {}).get(k)==v for k,v in w.items())
if args[:2]==["get","namespace"]:
    if args[2] not in load("namespaces.json"): raise SystemExit(1)
    print("namespace/"+args[2]); raise SystemExit(0)
if args[:2]==["get","pods"]:
    items=load("pods.json")["items"]; w=sel()
    if "--all-namespaces" in args:
        node=args[args.index("--field-selector")+1].split("=",1)[1]
        items=[x for x in items if x.get("spec",{}).get("nodeName")==node and matches(x,w)]
        print("\n".join(x["metadata"]["namespace"] for x in items)); raise SystemExit(0)
    items=[x for x in items if x["metadata"]["namespace"]==ns() and matches(x,w)]
    print("\n".join("pod/"+x["metadata"]["name"] for x in items)); raise SystemExit(0)
if args[:2]==["get","deployment"]:
    name=args[2]
    found=[x for x in load("deployments.json")["items"] if x["metadata"]["namespace"]==ns() and x["metadata"]["name"]==name]
    if not found: raise SystemExit(1)
    print("deployment.apps/"+name); raise SystemExit(0)
if args[:2]==["get","network-attachment-definitions.k8s.cni.cncf.io"]:
    name=args[2]
    found=[x for x in load("nads.json")["items"] if x["metadata"]["namespace"]==ns() and x["metadata"]["name"]==name]
    if not found: raise SystemExit(1)
    print("network-attachment-definition.k8s.cni.cncf.io/"+name); raise SystemExit(0)
if args[:2]==["delete","deployment"]:
    name=args[2]; data=load("deployments.json")
    data["items"]=[x for x in data["items"] if not (x["metadata"]["namespace"]==ns() and x["metadata"]["name"]==name)]
    save("deployments.json",data); print("deleted"); raise SystemExit(0)
if args[:2]==["delete","pods"]:
    data=load("pods.json"); w=sel()
    data["items"]=[x for x in data["items"] if not (x["metadata"]["namespace"]==ns() and matches(x,w))]
    save("pods.json",data); print("deleted"); raise SystemExit(0)
if args[:2]==["delete","network-attachment-definitions.k8s.cni.cncf.io"]:
    name=args[2]; data=load("nads.json")
    data["items"]=[x for x in data["items"] if not (x["metadata"]["namespace"]==ns() and x["metadata"]["name"]==name)]
    save("nads.json",data); print("deleted"); raise SystemExit(0)
raise SystemExit(2)
'''

PLAYBOOK="""---
- hosts: ran_node
  gather_facts: false
  environment:
    PATH: "{{ fixture_bin }}:{{ lookup('env','PATH') }}"
    SYNTHRAN_RAN_TRANSITION_FIXTURE: "{{ fixture_state }}"
  vars:
    platform: r2lab
    rru: n320
    ran: "{{ fixture_ran }}"
    core: "{{ fixture_core }}"
    ran_node_name: ran-ci
    helm_version: "3.22.0"
    run_dir: "{{ fixture_run }}"
    synthran_host_preparation: preserve
  roles:
    - role: 5g/ran_transition
"""
INVENTORY="""---
all:
  children:
    ran_node:
      hosts:
        ran-ci:
          ansible_connection: local
          ansible_python_interpreter: __PYTHON__
"""

def dep(ns,name): return {"metadata":{"namespace":ns,"name":name}}
def pod(ns,name,labels,node="ran-ci"): return {"metadata":{"namespace":ns,"name":name,"labels":labels},"spec":{"nodeName":node}}
def nad(ns,name): return {"metadata":{"namespace":ns,"name":name}}

def run_fixture(selected:str, core:str, state:dict) -> tuple[dict,dict,str]:
    ansible=shutil.which("ansible-playbook"); require(ansible is not None,"ansible-playbook missing")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); shutil.copytree(ROOT/"deployment/roles/5g/ran_transition",root/"roles/5g/ran_transition")
        bindir=root/"bin"; bindir.mkdir(); statedir=root/"state"; statedir.mkdir(); rundir=root/"run"; rundir.mkdir()
        for n,v in state.items(): (statedir/f"{n}.json").write_text(json.dumps(v))
        (statedir/"uninstalls.log").write_text("")
        for n,s in (("helm",HELM),("kubectl",KUBECTL)):
            p=bindir/n; p.write_text(s); make_executable(p)
        inv=root/"inventory.yml"; inv.write_text(INVENTORY.replace("__PYTHON__",sys.executable))
        pb=root/"play.yml"; pb.write_text(PLAYBOOK)
        env=os.environ.copy(); env["ANSIBLE_ROLES_PATH"]=str(root/"roles"); env["ANSIBLE_NOCOLOR"]="1"
        cp=subprocess.run([ansible,"-i",str(inv),str(pb),"-e",f"fixture_ran={selected}","-e",f"fixture_core={core}","-e",f"fixture_state={statedir}","-e",f"fixture_bin={bindir}","-e",f"fixture_run={rundir}"],cwd=ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        require(cp.returncode==0,cp.stdout)
        evidence=json.loads((rundir/"provenance/ran-transition.json").read_text())
        final={n:json.loads((statedir/f"{n}.json").read_text()) for n in state}
        return evidence,final,(statedir/"uninstalls.log").read_text()

def base():
    return {"namespaces":["oai","open5gs","other"],"releases":[],"deployments":{"items":[]},"pods":{"items":[]},"nads":{"items":[]}}

def check_cross_namespace_oai_to_srsran():
    s=base()
    s["releases"]=[
        {"name":"oai-gnb","namespace":"oai","status":"deployed"},
        {"name":"oai-cu","namespace":"oai","status":"deployed"},
        {"name":"srsran-gnb","namespace":"open5gs","status":"deployed"},
        {"name":"oai-flexric","namespace":"oai","status":"deployed"},
        {"name":"oai-gnb","namespace":"other","status":"deployed"},
    ]
    s["deployments"]["items"]=[dep("oai","oai-gnb"),dep("oai","oai-cu"),dep("open5gs","srsran-gnb"),dep("other","oai-gnb")]
    s["pods"]["items"]=[
        pod("oai","old-oai",{"app.kubernetes.io/instance":"oai-gnb"}),
        pod("open5gs","selected-srs",{"app":"srsran","component":"gnb"}),
        pod("other","other-oai",{"app.kubernetes.io/instance":"oai-gnb"},node="other-node"),
    ]
    s["nads"]["items"]=[nad("oai","oai-gnb-ru"),nad("open5gs","ru-network"),nad("other","oai-gnb-ru")]
    e,f,log=run_fixture("srsran","open5gs",s)
    keys={(x["namespace"],x["name"]) for x in f["releases"]}
    require(("oai","oai-gnb") not in keys,"old cross-core OAI gNB survived")
    require(("oai","oai-cu") not in keys,"stale OAI RAN family was only partially cleaned")
    require(("other","oai-gnb") in keys,"unrelated-node namespace was mutated")
    require(("oai","oai-flexric") in keys,"FlexRIC was stolen by RAN transition")
    require(("open5gs","srsran-gnb") in keys,"selected stack was removed")
    require("uninstall oai-gnb --namespace oai" in log,"old namespace gNB release not removed")
    require("uninstall oai-cu --namespace oai" in log,"full stale RAN family was not cleaned")
    require(e["exclusive_prelaunch"] is True,"missing exclusivity evidence")

def check_srsran_to_oai_and_nonran_preservation():
    s=base()
    s["releases"]=[
        {"name":"srsran-gnb","namespace":"open5gs","status":"deployed"},
        {"name":"srsran-ue","namespace":"open5gs","status":"deployed"},
        {"name":"oai-gnb","namespace":"oai","status":"deployed"},
    ]
    s["deployments"]["items"]=[dep("open5gs","srsran-gnb"),dep("oai","oai-gnb")]
    s["pods"]["items"]=[
        pod("open5gs","old-srs",{"app":"srsran","component":"gnb"}),
        pod("open5gs","srs-ue",{"app":"srsran","component":"ue"}),
        pod("oai","selected-oai",{"app.kubernetes.io/instance":"oai-gnb"}),
    ]
    s["nads"]["items"]=[nad("open5gs","ru-network"),nad("oai","oai-gnb-ru")]
    _,f,log=run_fixture("oai","oai",s)
    keys={(x["namespace"],x["name"]) for x in f["releases"]}
    require(("open5gs","srsran-gnb") not in keys,"old srsRAN survived")
    require(("open5gs","srsran-ue") in keys,"srsUE lifecycle was stolen")
    require(("oai","oai-gnb") in keys,"selected OAI was removed")
    require("uninstall srsran-gnb --namespace open5gs" in log,"old srsRAN namespace not removed")

def check_orphan_without_helm():
    s=base()
    s["deployments"]["items"]=[dep("oai","oai-gnb")]
    s["pods"]["items"]=[pod("oai","orphan",{"app.kubernetes.io/instance":"oai-gnb"})]
    s["nads"]["items"]=[nad("oai","oai-gnb-ru")]
    e,f,log=run_fixture("srsran","open5gs",s)
    require(log=="","orphan cleanup invented Helm ownership")
    require(f["deployments"]["items"]==[],"orphan deployment survived")
    require(f["pods"]["items"]==[],"orphan pod survived")
    require(f["nads"]["items"]==[],"orphan RU NAD survived")
    require(e["exclusive_prelaunch"] is True,"orphan cleanup was not accepted")

def main():
    static_contract()
    check_cross_namespace_oai_to_srsran()
    check_srsran_to_oai_and_nonran_preservation()
    check_orphan_without_helm()
    print("shared physical cross-RAN transition contract OK")

if __name__=="__main__":
    main()
