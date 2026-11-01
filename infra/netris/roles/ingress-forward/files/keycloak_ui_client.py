#!/usr/bin/env python3
"""Allow the forwarded-ingress callback on the osac-ui Keycloak client.

The realm import hardcodes the osac-ui redirect URIs to the Kind development
UI (http://ui.osac.localhost:8080), and the installer has no value for the
lab's real UI route. This script merges the forwarded-ingress callback into the
existing client, preserving every redirect URI and web origin that is already
there, and only writes the client when something is missing.

It reads the Keycloak admin credentials from the keycloak-service deployment so
they never travel through Ansible. It never prints credentials, tokens or the
client secret.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse


def run(cmd: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(f"command failed: {' '.join(cmd[:2])}")
    return result.stdout


def curl(resolve: str, args: list[str], *, stdin: str | None = None) -> str:
    return run(["curl", "-sk", "--fail-with-body", "--resolve", resolve, *args], stdin=stdin)


def read_admin_credentials() -> tuple[str, str]:
    deployment = json.loads(
        run(["oc", "-n", "keycloak", "get", "deployment", "keycloak-service", "-o", "json"])
    )
    env = {
        item["name"]: item.get("value", "")
        for item in deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])
    }
    return env.get("KEYCLOAK_ADMIN", "admin"), env.get("KEYCLOAK_ADMIN_PASSWORD", "admin")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keycloak-host", required=True)
    parser.add_argument("--keycloak-ip", required=True)
    parser.add_argument("--keycloak-port", type=int, required=True)
    parser.add_argument("--realm", default="osac")
    parser.add_argument("--client-id", default="osac-ui")
    parser.add_argument("--redirect-uri", required=True)
    parser.add_argument("--redirect-glob", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()

    base = f"https://{args.keycloak_host}:{args.keycloak_port}"
    resolve = f"{args.keycloak_host}:{args.keycloak_port}:{args.keycloak_ip}"
    username, password = read_admin_credentials()

    token = json.loads(
        curl(
            resolve,
            [
                "-X",
                "POST",
                f"{base}/realms/master/protocol/openid-connect/token",
                "--data-binary",
                "@-",
            ],
            stdin=urllib.parse.urlencode(
                {
                    "client_id": "admin-cli",
                    "username": username,
                    "password": password,
                    "grant_type": "password",
                }
            ),
        )
    ).get("access_token", "")
    if not token:
        raise SystemExit("Keycloak returned no admin access token")

    clients = json.loads(
        curl(
            resolve,
            [
                "-H",
                f"Authorization: Bearer {token}",
                f"{base}/admin/realms/{args.realm}/clients?clientId={args.client_id}",
            ],
        )
    )
    if not clients:
        raise SystemExit(f"Keycloak client {args.client_id!r} was not found")

    client = clients[0]
    redirect_uris = list(client.get("redirectUris") or [])
    web_origins = list(client.get("webOrigins") or [])

    missing = 0
    for uri in (args.redirect_uri, args.redirect_glob):
        if uri not in redirect_uris:
            redirect_uris.append(uri)
            missing += 1
    if args.origin not in web_origins:
        web_origins.append(args.origin)
        missing += 1

    if missing == 0:
        print("unchanged")
        return 0

    client["redirectUris"] = redirect_uris
    client["webOrigins"] = web_origins
    curl(
        resolve,
        [
            "-X",
            "PUT",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: application/json",
            f"{base}/admin/realms/{args.realm}/clients/{client['id']}",
            "--data-binary",
            "@-",
        ],
        stdin=json.dumps(client),
    )
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
