#!/usr/bin/env python3
"""Reduce `oc get virtualmachines|virtualmachineinstances -o json` (stdin) to an allow-list.

A VM spec can carry cloud-init user data with passwords, so nothing is exported
unless it is listed here. Per item: metadata name, namespace, uid,
creationTimestamp and labels; spec.runStrategy; the networks and
domain.devices.interfaces of the VM template spec (spec.template.spec) or of the
VMI spec; and the status keys listed in VM_STATUS / VMI_STATUS. Everything is an
allow-list, so a field added to the API later is not exported until it is added
here. Condition and volume messages are free text and are exported as they are;
the release gate and the collector redaction still scan the file.

Exits 1 and writes nothing when the input is not a JSON object."""
import json
import sys

METADATA = ("name", "namespace", "uid", "creationTimestamp", "labels")
# Top-level status keys that are exported, per kind. Anything else is dropped.
VM_STATUS = ("printableStatus", "ready", "created", "conditions", "observedGeneration", "desiredGeneration",
             "startFailure")
VMI_STATUS = ("phase", "phaseTransitionTimestamps", "reason", "nodeName", "conditions", "interfaces",
              "guestOSInfo", "migrationState", "volumeStatus")


def pick(source, keys):
    return {key: source[key] for key in keys if isinstance(source, dict) and key in source}


def machine(spec):
    """networks and devices.interfaces of a VMI-style spec."""
    spec = spec if isinstance(spec, dict) else {}
    out = pick(spec, ("networks",))
    devices = (spec.get("domain") or {}).get("devices") if isinstance(spec.get("domain"), dict) else None
    interfaces = pick(devices, ("interfaces",))
    if interfaces:
        out["domain"] = {"devices": interfaces}
    return out


def export_item(item):
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    out = {"metadata": pick(item.get("metadata"), METADATA), "spec": pick(spec, ("runStrategy",))}
    if isinstance(spec.get("template"), dict):  # VirtualMachine
        template = machine(spec["template"].get("spec"))
        if template:
            out["spec"]["template"] = {"spec": template}
    else:  # VirtualMachineInstance
        out["spec"].update(machine(spec))
    if isinstance(item.get("status"), dict):
        out["status"] = pick(item["status"], VM_STATUS if isinstance(spec.get("template"), dict) else VMI_STATUS)
    return out


def main() -> int:
    try:
        document = json.load(sys.stdin)
    except ValueError:
        return 1
    if not isinstance(document, dict):
        return 1
    items = document.get("items")
    result = {"items": [export_item(i) for i in items if isinstance(i, dict)]} if isinstance(items, list) \
        else export_item(document)
    json.dump(result, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
