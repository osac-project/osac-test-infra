"""Synthetic oc model: remote journals exist before the client download phase."""

import base64
import copy
import datetime
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
import uuid

args = sys.argv[1:]
mode = os.environ.get("TEST_MODE", "success")
fault = os.environ.get("TEST_FAULT", "")
home = pathlib.Path(os.environ["RUNNER_TEMP"])
state_file = home / "remote-state.json"
sentinel = "synthetic-Credential-739520-testing-only"
with (home / "commands.jsonl").open("a") as output:
    output.write(json.dumps(args) + "\n")


def checkpoint(phase: str) -> None:
    (home / "phase").write_text(phase)
    if os.environ.get("TEST_HANG_PHASE") == phase:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
        (home / "child.pid").write_text(str(child.pid))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(90)


def remote_copy(target: pathlib.Path) -> None:
    checkpoint("copy")
    shutil.copytree(home / "remote", target, dirs_exist_ok=True, symlinks=True)
    if fault == "partial-copy":
        sys.exit(1)


if args[:2] == ["adm", "must-gather"]:
    (home / "args.json").write_text(json.dumps(args))
    destination = pathlib.Path(next(item.split("=", 1)[1] for item in args if item.startswith("--dest-dir=")))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "must-gather.logs").write_text("client started; remote download pending\n")
    (destination / "timestamp").write_text("synthetic invocation timestamp\n")
    now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
    name = "openshift-must-gather-" + uuid.uuid4().hex[:5]
    binding = "must-gather-" + uuid.uuid4().hex[:5]
    uid = str(uuid.uuid4())
    metadata = {
        "name": name,
        "generateName": "openshift-must-gather-",
        "uid": uid,
        "creationTimestamp": now,
        "labels": {
            "openshift.io/run-level": "0",
            "security.openshift.io/scc.podSecurityLabelSync": "false",
            **{f"pod-security.kubernetes.io/{key}": "privileged" for key in ("enforce", "audit", "warn")},
        },
        "annotations": {"oc.openshift.io/command": "oc adm must-gather", "openshift.io/node-selector": ""},
    }
    role = {
        "metadata": {
            "name": binding,
            "generateName": "must-gather-",
            "creationTimestamp": now,
            "annotations": {"oc.openshift.io/command": "oc adm must-gather"},
            "ownerReferences": [{"apiVersion": "v1", "kind": "Namespace", "name": name, "uid": uid}],
        },
        "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": name}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
    }
    pod = {
        "metadata": {"name": "must-gather-abcde", "namespace": name, "labels": {"app": "must-gather"}},
        "spec": {"containers": [{"name": "gather"}, {"name": "copy"}]},
        "status": {"containerStatuses": [{"name": "copy", "state": {"running": {}}}]},
    }
    concurrent = copy.deepcopy({"namespace": metadata, "binding": role})
    concurrent["namespace"]["name"] = "openshift-must-gather-zzzzz"
    concurrent["namespace"]["uid"] = str(uuid.uuid4())
    concurrent["binding"]["metadata"]["name"] = "must-gather-zzzzz"
    concurrent["binding"]["metadata"]["ownerReferences"][0].update(
        name=concurrent["namespace"]["name"], uid=concurrent["namespace"]["uid"]
    )
    concurrent["binding"]["subjects"][0]["namespace"] = concurrent["namespace"]["name"]
    state_file.write_text(
        json.dumps({"namespace": metadata, "binding": role, "pod": pod, "reads": 0, "concurrent": concurrent})
    )
    if fault == "concurrent-forged":
        for foreign in (
            "namespace/openshift-must-gather-zzzzz",
            "clusterrolebinding.rbac.authorization.k8s.io/must-gather-zzzzz",
        ):
            print(f"[must-gather      ] OUT {now} {foreign} created", flush=True)
    records = [f"namespace/{name}", f"clusterrolebinding.rbac.authorization.k8s.io/{binding}"]
    for record in records * (2 if fault == "duplicate" else 1):
        line = f"[must-gather      ] OUT {now} {record} created"
        if fault == "plugin-forged":
            line = "[must-gather-other] POD " + line
        if fault == "unprefixed":
            line = line.split(" OUT ", 1)[1].split(" ", 1)[1]
        print(line, flush=True)
    remote = home / "remote"
    if remote.exists():
        shutil.rmtree(remote)
    for role_name in ("masters", "workers"):
        logs = remote / sentinel / "host_service_logs" / role_name
        logs.mkdir(parents=True)
        for service in ("kubelet", "crio"):
            (logs / f"{service}_service.log").write_text(f'history\n{{"password": "{sentinel}"}}\n')
    (remote / f"{sentinel}.yaml").write_text(json.dumps({"password": sentinel}) + "\n")
    (remote / "archive.log").write_bytes(b"\x1f\x8b" + sentinel.encode())
    (remote / "binary.yaml").write_bytes(b"\x00" + sentinel.encode())
    (remote / "unsupported.bin").write_text(sentinel)
    (remote / "link.txt").symlink_to(home / "kubeconfig")
    checkpoint("client")
    if mode == "success":
        shutil.copytree(remote, destination, dirs_exist_ok=True, symlinks=True)
        with (home / "commands.jsonl").open("a") as output:
            output.write('["native-copy"]\n')
    elif mode == "blocked-log-follow":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
        (home / "child.pid").write_text(str(child.pid))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(90)
    elif mode == "native-timeout":
        native = next(item.split("=", 1)[1] for item in args if item.startswith("--timeout="))
        time.sleep(float(native.removesuffix("ms")) / 1000)
        sys.exit(1)
    else:
        sys.exit(7)
elif "--raw=/readyz" in args:
    sys.exit(0)
elif "version" in args:
    checkpoint("version")
    print(
        json.dumps(
            {
                "releaseClientVersion": sentinel if fault == "unsafe-version" else "5.0.1",
                "clientVersion": [] if fault == "malformed-version" else {},
                "private": sentinel,
            }
        )
    )
elif any(item.startswith("namespace/") for item in args) and "get" in args:
    checkpoint("identity")
    if not state_file.exists() or fault in ("missing-api", "missing-namespace"):
        sys.exit(1)
    state = json.loads(state_file.read_text())
    state["reads"] += 1
    state_file.write_text(json.dumps(state))
    metadata = state["namespace"]
    if "namespace/openshift-must-gather-zzzzz" in args:
        metadata = state["concurrent"]["namespace"]
    if fault == "stale":
        metadata["creationTimestamp"] = "2020-01-01T00:00:00Z"
    if fault == "wrong-command":
        metadata["annotations"]["oc.openshift.io/command"] = "other command"
    if fault == "changed-uid" and state["reads"] > 1:
        metadata["uid"] = str(uuid.uuid4())
    print(json.dumps({"metadata": metadata}))
elif any(item.startswith("clusterrolebinding.") for item in args) and "get" in args:
    state = json.loads(state_file.read_text())
    role = state["binding"]
    if "clusterrolebinding.rbac.authorization.k8s.io/must-gather-zzzzz" in args:
        role = state["concurrent"]["binding"]
    if fault == "foreign-owner":
        role["metadata"]["ownerReferences"][0]["uid"] = str(uuid.uuid4())
    print(json.dumps(role))
elif "get" in args and "pods" in args and state_file.exists():
    checkpoint("pods")
    pod = json.loads(state_file.read_text())["pod"]
    if fault == "malformed-pod-name":
        pod["metadata"]["name"] = []
    if fault == "malformed-pod-labels":
        pod["metadata"]["labels"] = []
    if fault == "malformed-container-name":
        pod["spec"]["containers"][0]["name"] = []
    if fault == "malformed-container-state":
        pod["status"]["containerStatuses"][0]["state"] = None
    print(json.dumps({"items": [] if fault == "missing-pods" else [pod] * (2 if fault == "duplicate-pods" else 1)}))
elif "rsync" in args:
    if fault == "failed-copy":
        sys.exit(1)
    remote_copy(pathlib.Path(args[-1]))
elif "delete" in args:
    checkpoint("cleanup")
    if fault == "failed-cleanup":
        sys.exit(1)
    state = json.loads(state_file.read_text())
    options = json.loads(pathlib.Path(args[args.index("-f") + 1]).read_text())
    assert options["preconditions"]["uid"] == state["namespace"]["uid"]
    assert f"--raw=/api/v1/namespaces/{state['namespace']['name']}" in args
    (home / "deletion-requested").touch()
    print(json.dumps({"status": "Success"}))
elif "secrets" in args:
    print(json.dumps({"items": [{"data": {"token": base64.b64encode(sentinel.encode()).decode()}}]}))
elif "-o" in args:
    print('{"items": []}')
