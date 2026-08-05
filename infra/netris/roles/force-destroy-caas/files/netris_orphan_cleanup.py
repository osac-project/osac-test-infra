#!/usr/bin/env python3
"""Remove orphan Netris order-* server clusters / VPCs / NAT / vnets.

Args: <netris_url> <cookie_jar> <user> <password> [force_order ...]
Env:  NETRIS_LIVE_ORDERS_FILE — newline-separated live ClusterOrder/HC names
"""
import json
import os
import subprocess
import sys

url, jar, user, password = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
force = [x for x in sys.argv[5:] if x]


def curl(method, path, body=None, fail_ok=False):
    cmd = [
        "curl", "-sk", "--connect-timeout", "15",
        "-b", jar, "-c", jar,
        "-w", "\nHTTP:%{http_code}",
        "-X", method, url + path,
    ]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        if fail_ok:
            return "000", ""
        raise
    body_out, _, code = out.rpartition("HTTP:")
    return code.strip(), body_out


subprocess.run(
    [
        "curl", "-sk", "-c", jar, "-X", "POST", f"{url}/api/auth",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"user": user, "password": password, "auth_scheme_id": 1}),
    ],
    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)


def get_list(path):
    code, raw = curl("GET", path, fail_ok=True)
    if code != "200":
        print(f"  ! GET {path} HTTP {code} — skip", file=sys.stderr)
        return []
    try:
        return json.loads(raw).get("data") or []
    except Exception:
        return []


live = set()
live_file = os.environ.get("NETRIS_LIVE_ORDERS_FILE", "")
if live_file and os.path.isfile(live_file):
    with open(live_file) as f:
        live = {ln.strip() for ln in f if ln.strip()}

scs = get_list("/api/v2/server-cluster")
vpcs = get_list("/api/v2/vpc")
nats = get_list("/api/v2/nat")


def is_order_name(name):
    return bool(name) and str(name).startswith("order-")


def should_delete(name):
    if not is_order_name(name):
        return False
    if force:
        return name in force
    return name not in live


targets_sc = [c for c in scs if should_delete(c.get("name"))]
targets_vpc = [
    v for v in vpcs
    if should_delete(v.get("name"))
    and not v.get("isSystem")
    and not v.get("isDefault")
    and v.get("name") not in ("Default", "ocp-sno")
]

print(f"  · live OSAC orders: {sorted(live) or '(none)'}")
if force:
    print(f"  · forced: {force}")
print(f"  · server clusters to delete: {[c.get('name') for c in targets_sc] or '(none)'}")
print(f"  · VPCs to delete: {[v.get('name') for v in targets_vpc] or '(none)'}")

if not targets_sc and not targets_vpc and not force:
    print("  ✓ nothing to clean")
    sys.exit(0)

deleted = 0
orphan_names = {c.get("name") for c in targets_sc} | {v.get("name") for v in targets_vpc} | set(force)
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
        any(o and o in name for o in orphan_names)
        or vpc.get("id") in orphan_vpc_ids
        or vpc.get("name") in orphan_names
    )
    if not hit:
        continue
    nid = n.get("id")
    print(f"  · delete NAT {nid} ({name})")
    code, _ = curl("DELETE", f"/api/v2/nat/{nid}", fail_ok=True)
    print(f"    HTTP {code}")
    if code.startswith("2"):
        deleted += 1

for c in targets_sc:
    cid, name = c.get("id"), c.get("name")
    servers = c.get("servers") or []
    print(f"  · delete server-cluster {cid} ({name}) servers={servers}")
    code, raw = curl("DELETE", f"/api/v2/server-cluster/{cid}", fail_ok=True)
    print(f"    HTTP {code}")
    if code.startswith("2"):
        deleted += 1
    else:
        print(f"    body: {raw[:240]}", file=sys.stderr)

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
    code, _ = curl("DELETE", f"/api/v2/vnet/{vid}", fail_ok=True)
    print(f"    HTTP {code}")
    if code.startswith("2"):
        deleted += 1

vpcs = get_list("/api/v2/vpc")
for v in vpcs:
    name = v.get("name") or ""
    if not should_delete(name):
        continue
    if v.get("isSystem") or v.get("isDefault") or name in ("Default", "ocp-sno"):
        continue
    vid = v.get("id")
    print(f"  · delete VPC {vid} ({name})")
    code, raw = curl("DELETE", f"/api/v2/vpc/{vid}", fail_ok=True)
    print(f"    HTTP {code}")
    if code.startswith("2"):
        deleted += 1
    else:
        print(f"    body: {raw[:240]}", file=sys.stderr)

print(f"  ✓ Netris orphan cleanup done ({deleted} deletes)")
