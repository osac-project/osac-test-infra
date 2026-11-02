#!/usr/bin/env python3
"""Remove orphan Netris order-* server clusters / VPCs / NAT / vnets.

Args: <netris_url> <cookie_jar> <user> <password> <order> [order ...] --orphan-nat-order <name-or-empty>
"""
import json
import re
import subprocess
import sys

url, jar, user, password = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
arguments = sys.argv[5:]
if len(arguments) < 2 or arguments[-2] != "--orphan-nat-order":
    raise SystemExit("Expected --orphan-nat-order <name> after the targeted orders")
force = [x for x in arguments[:-2] if x]
orphan_nat_order = arguments[-1]
if orphan_nat_order and not re.fullmatch(r"order-[a-z0-9]+", orphan_nat_order):
    raise SystemExit("The orphan NAT order must be an order-* name")
if not force and not orphan_nat_order:
    raise SystemExit("Refusing unscoped Netris cleanup: provide a targeted order")


def curl(method, path, body=None):
    cmd = [
        "curl", "-sk", "--connect-timeout", "15",
        "-b", jar, "-c", jar,
        "-w", "\nHTTP:%{http_code}",
        "-X", method, url + path,
    ]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
    body_out, _, code = out.rpartition("HTTP:")
    return code.strip(), body_out


subprocess.run(
    [
        "curl", "-sk", "-c", jar, "-X", "POST", f"{url}/api/auth",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"user": user, "password": password, "auth_scheme_id": 1}),
    ],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)


def get_list(path):
    code, raw = curl("GET", path)
    if code != "200":
        raise SystemExit(f"Netris GET {path} failed (HTTP {code})")
    result = json.loads(raw)
    if not result.get("isSuccess") or not isinstance(result.get("data"), list):
        raise SystemExit(f"Netris GET {path} returned an invalid response")
    return result["data"]


scs = get_list("/api/v2/server-cluster")
vpcs = get_list("/api/v2/vpc")
nats = get_list("/api/v2/nat")
if orphan_nat_order:
    active_orders = {
        item["metadata"]["name"]
        for resource in ("clusterorders", "hostedclusters")
        for item in json.loads(subprocess.check_output(
            ["oc", "get", resource, "-A", "-o", "json"]
        ))["items"]
    }
    if orphan_nat_order in active_orders:
        raise SystemExit(f"Refusing to remove NAT for active order {orphan_nat_order}")


def is_order_name(name):
    return bool(name) and str(name).startswith("order-")


def should_delete(name):
    return is_order_name(name) and name in force


targets_sc = [c for c in scs if should_delete(c.get("name"))]
targets_vpc = [
    v for v in vpcs
    if should_delete(v.get("name"))
    and not v.get("isSystem")
    and not v.get("isDefault")
    and v.get("name") not in ("Default", "ocp-sno")
]

print(f"  · targeted orders: {force}")
print(f"  · server clusters to delete: {[c.get('name') for c in targets_sc] or '(none)'}")
print(f"  · VPCs to delete: {[v.get('name') for v in targets_vpc] or '(none)'}")

deleted = 0
orphan_names = {c.get("name") for c in targets_sc} | {v.get("name") for v in targets_vpc} | set(force)
nat_orders = set(force) | ({orphan_nat_order} if orphan_nat_order else set())
orphan_vpc_ids = set()
for v in targets_vpc:
    if v.get("id") is not None:
        orphan_vpc_ids.add(v["id"])
for c in targets_sc:
    vpc = c.get("vpc") or {}
    if vpc.get("id") is not None:
        orphan_vpc_ids.add(vpc["id"])
    if vpc.get("name"):
        orphan_names.add(vpc["name"])

for n in nats:
    name = n.get("name") or ""
    vpc = n.get("vpc") or {}
    hit = (
        any(name.startswith(f"{order}-") for order in nat_orders)
        or vpc.get("id") in orphan_vpc_ids
        or vpc.get("name") in orphan_names
    )
    if not hit:
        continue
    nid = n.get("id")
    print(f"  · delete NAT {nid} ({name})")
    code, _ = curl("DELETE", f"/api/v2/nat/{nid}")
    print(f"    HTTP {code}")
    if code.startswith("2") or code == "404":
        deleted += 1
    else:
        raise SystemExit(f"Netris NAT deletion failed for {name} (HTTP {code})")

for c in targets_sc:
    cid, name = c.get("id"), c.get("name")
    servers = c.get("servers") or []
    print(f"  · delete server-cluster {cid} ({name}) servers={servers}")
    code, _ = curl("DELETE", f"/api/v2/server-cluster/{cid}")
    print(f"    HTTP {code}")
    if code.startswith("2"):
        deleted += 1
    else:
        raise SystemExit(f"Netris server-cluster deletion failed for {name} (HTTP {code})")

vnets = get_list("/api/v2/vnet")
for vn in vnets:
    name = vn.get("name") or ""
    vpc = vn.get("vpc") or {}
    hit = (
        any(o and o in name for o in orphan_names)
        or vpc.get("id") in orphan_vpc_ids
        or vpc.get("name") in orphan_names
    )
    if not hit:
        continue
    vid = vn.get("id")
    print(f"  · delete vnet {vid} ({name})")
    code, _ = curl("DELETE", f"/api/v2/vnet/{vid}")
    print(f"    HTTP {code}")
    if code.startswith("2") or code == "404":
        deleted += 1
    else:
        raise SystemExit(f"Netris V-Net deletion failed for {name} (HTTP {code})")

vpcs = get_list("/api/v2/vpc")
for v in vpcs:
    name = v.get("name") or ""
    if not should_delete(name):
        continue
    if v.get("isSystem") or v.get("isDefault") or name in ("Default", "ocp-sno"):
        continue
    vid = v.get("id")
    print(f"  · delete VPC {vid} ({name})")
    code, _ = curl("DELETE", f"/api/v2/vpc/{vid}")
    print(f"    HTTP {code}")
    if code.startswith("2"):
        deleted += 1
    else:
        raise SystemExit(f"Netris VPC deletion failed for {name} (HTTP {code})")

remaining = [n["name"] for n in get_list("/api/v2/nat")
             if any(n.get("name", "").startswith(f"{order}-") for order in nat_orders)]
if remaining:
    raise SystemExit(f"Netris NAT cleanup incomplete: {remaining}")

print(f"  ✓ Netris orphan cleanup done ({deleted} deletes)")
