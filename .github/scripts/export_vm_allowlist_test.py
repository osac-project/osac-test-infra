"""Test for infra/netris/roles/gather-vmaas/files/export_vm_allowlist.py."""
import json
import pathlib
import subprocess
import sys

FILTER = pathlib.Path(__file__).resolve().parents[2] / "infra/netris/roles/gather-vmaas/files/export_vm_allowlist.py"


def run(text: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(FILTER)], input=text, capture_output=True, text=True, check=False)


def main() -> None:
    vm = {
        "apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
        "metadata": {"name": "vm1", "namespace": "ns1", "uid": "u-1", "creationTimestamp": "2026-01-01T00:00:00Z",
                     "labels": {"app": "x"}, "annotations": {"note": "ANNOTATION_SECRET"}, "managedFields": [{"f": "MANAGED"}]},
        "spec": {
            "runStrategy": "Always",
            "futureField": {"token": "FUTURE_SECRET"},  # unknown new field
            "template": {"spec": {
                "networks": [{"name": "default", "multus": {"networkName": "nad"}}],
                "domain": {"devices": {"interfaces": [{"name": "default", "bridge": {}}], "disks": [{"name": "d"}]}},
                "volumes": [{"name": "cloudinitdisk", "cloudInitNoCloud": {"userData": "#cloud-config\npassword: USERDATA_SECRET"}}],
            }},
        },
        "status": {"printableStatus": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                   "futureField": {"token": "STATUS_FUTURE_SECRET"},
                   "volumes": [{"cloudInitNoCloud": {"userData": "STATUS_SECRET"}, "name": "v"}]},
    }
    vmi = {"metadata": {"name": "vmi1", "namespace": "ns1"},
           "spec": {"networks": [{"name": "default"}], "domain": {"devices": {"interfaces": [{"name": "default"}]}},
                    "volumes": [{"cloudInitNoCloud": {"userDataBase64": "VMI_SECRET"}}]},
           "status": {"phase": "Running", "interfaces": [{"ipAddress": "10.0.0.5"}], "futureField": "VMI_FUTURE_SECRET"}}
    done = run(json.dumps({"items": [vm, vmi]}))
    assert done.returncode == 0, done.stderr
    for secret in ("USERDATA_SECRET", "FUTURE_SECRET", "STATUS_SECRET", "VMI_SECRET", "ANNOTATION_SECRET", "MANAGED",
                   "cloudInit", "userData", "futureField", "disks", "STATUS_FUTURE_SECRET", "VMI_FUTURE_SECRET"):
        assert secret not in done.stdout, f"{secret} was exported"
    first, second = json.loads(done.stdout)["items"]
    assert first["metadata"] == {"name": "vm1", "namespace": "ns1", "uid": "u-1",
                                 "creationTimestamp": "2026-01-01T00:00:00Z", "labels": {"app": "x"}}, first["metadata"]
    assert first["spec"]["runStrategy"] == "Always"
    template = first["spec"]["template"]["spec"]
    assert template["networks"][0]["name"] == "default" and template["domain"]["devices"]["interfaces"][0]["name"] == "default"
    assert first["status"]["printableStatus"] == "Running" and first["status"]["conditions"], first["status"]
    assert second["spec"]["networks"] == [{"name": "default"}], second["spec"]
    assert second["status"] == {"phase": "Running", "interfaces": [{"ipAddress": "10.0.0.5"}]}, second["status"]
    assert set(first["status"]) == {"printableStatus", "conditions"}, first["status"]

    for bad in ("<html>502</html>", "[1, 2]"):
        result = run(bad)
        assert result.returncode == 1 and result.stdout == "", f"{bad!r} must produce no output"
    print("export_vm_allowlist_test.py: ok")


if __name__ == "__main__":
    main()
