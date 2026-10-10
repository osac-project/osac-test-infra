#!/usr/bin/env bash
set -euo pipefail

: "${KUBECONFIG:?KUBECONFIG is required}"
: "${OSAC_NAMESPACE:?OSAC_NAMESPACE is required}"
: "${OSAC_TENANT_NAME:?OSAC_TENANT_NAME is required}"
: "${OSAC_INTERNAL_ADDRESS:?OSAC_INTERNAL_ADDRESS is required}"
: "${OSAC_PUBLIC_ADDRESS:?OSAC_PUBLIC_ADDRESS is required}"
: "${KEYCLOAK_URL:?KEYCLOAK_URL is required}"
: "${KEYCLOAK_ADMIN_USER:?KEYCLOAK_ADMIN_USER is required}"
: "${KEYCLOAK_ADMIN_PASSWORD:?KEYCLOAK_ADMIN_PASSWORD is required}"
: "${TENANT_ADMIN_USER:?TENANT_ADMIN_USER is required}"
: "${TENANT_ADMIN_PASSWORD:?TENANT_ADMIN_PASSWORD is required}"
: "${TENANT_USER:?TENANT_USER is required}"
: "${TENANT_USER_PASSWORD:?TENANT_USER_PASSWORD is required}"
: "${TENANT_PROJECT:?TENANT_PROJECT is required}"

for binary in oc osac curl jq python3; do
  command -v "${binary}" >/dev/null || { echo "Required command not found: ${binary}" >&2; exit 1; }
done

WORK_DIR=$(mktemp -d /tmp/osac-tenant-onboard.XXXXXX)
chmod 700 "${WORK_DIR}"
trap 'rm -rf "${WORK_DIR}"' EXIT
CPA_CONFIG="${WORK_DIR}/cpa"
ADMIN_CONFIG="${WORK_DIR}/tenant-admin"
USER_CONFIG="${WORK_DIR}/tenant-user"
mkdir -m 700 "${CPA_CONFIG}" "${ADMIN_CONFIG}" "${USER_CONFIG}"
printf '%s' "${TENANT_ADMIN_PASSWORD}" > "${WORK_DIR}/admin-password"
printf '%s' "${TENANT_USER_PASSWORD}" > "${WORK_DIR}/user-password"
printf '%s' "${KEYCLOAK_ADMIN_PASSWORD}" > "${WORK_DIR}/keycloak-password"
chmod 600 "${WORK_DIR}"/*password

changed=0
osac_cpa() { osac --config "${CPA_CONFIG}" "$@"; }
osac_tenant() { osac --config "${CPA_CONFIG}" --tenant "${OSAC_TENANT_NAME}" "$@"; }

# Keep CPA's normal ~/.config/osac credentials untouched.
osac_cpa login --insecure --private --address "${OSAC_INTERNAL_ADDRESS}" \
  --token-script "oc create token -n ${OSAC_NAMESPACE} admin" >/dev/null

json_items() {
  jq -e 'if type == "array" then .
         elif type == "object" then (if has("items") then (.items // []) else [.] end)
         else error("Expected an OSAC JSON object or array") end
         | if type == "array" then . else error("Expected an OSAC items array") end'
}

tenants=$(osac_cpa get tenants -o json | json_items)
tenant=$(jq -c --arg name "${OSAC_TENANT_NAME}" '[.[] | select(.metadata.name == $name)][0] // {}' <<<"${tenants}")
if jq -e '.metadata.deletion_timestamp // empty' >/dev/null <<<"${tenant}"; then
  echo "Tenant ${OSAC_TENANT_NAME} is still deleting; wait for deletion to finish before setup." >&2
  exit 1
fi

if [[ "${tenant}" == '{}' ]]; then
  cat > "${WORK_DIR}/tenant.yaml" <<EOF
"@type": type.googleapis.com/osac.private.v1.Tenant
metadata:
  name: ${OSAC_TENANT_NAME}
EOF
  osac_cpa create -f "${WORK_DIR}/tenant.yaml" >/dev/null
  changed=1
fi

for attempt in $(seq 1 60); do
  tenant=$(osac_cpa get tenant "${OSAC_TENANT_NAME}" -o json)
  state=$(jq -r '.status.state // empty' <<<"${tenant}")
  [[ "${state}" == TENANT_STATE_SYNCED || "${state}" == SYNCED ]] && break
  [[ "${attempt}" -eq 60 ]] && { echo "Tenant did not reach SYNCED (state=${state:-unknown})." >&2; exit 1; }
  sleep 5
done

KC_ADMIN_PASSWORD_FILE="${WORK_DIR}/keycloak-password"
kc_token_response=$(curl -skS -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=password' \
  --data-urlencode 'client_id=admin-cli' \
  --data-urlencode "username=${KEYCLOAK_ADMIN_USER}" \
  --data-urlencode "password@${KC_ADMIN_PASSWORD_FILE}")
KC_ADMIN_TOKEN=$(jq -r '.access_token // empty' <<<"${kc_token_response}")
[[ -n "${KC_ADMIN_TOKEN}" ]] || { echo "Could not obtain Keycloak administrator token." >&2; exit 1; }
KC_AUTH_HEADER="${WORK_DIR}/keycloak-header"
printf 'header = "Authorization: Bearer %s"\n' "${KC_ADMIN_TOKEN}" > "${KC_AUTH_HEADER}"
chmod 600 "${KC_AUTH_HEADER}"
KC_BASE="${KEYCLOAK_URL%/}/admin/realms/${KEYCLOAK_REALM:-osac}"

kc_admin_call() {
  curl -skS --config "${KC_AUTH_HEADER}" "$@"
}

for attempt in $(seq 1 30); do
  orgs=$(kc_admin_call "${KC_BASE}/organizations?search=${OSAC_TENANT_NAME}")
  org_id=$(jq -r --arg name "${OSAC_TENANT_NAME}" '.[] | select(.name == $name) | .id' <<<"${orgs}" | head -1)
  [[ -n "${org_id}" ]] && break
  [[ "${attempt}" -eq 30 ]] && { echo "Keycloak organization ${OSAC_TENANT_NAME} was not created." >&2; exit 1; }
  sleep 3
done

kc_ensure_user() {
  local username="$1" password_file="$2" email="$3" users user_id body status
  users=$(kc_admin_call -G --data-urlencode "username=${username}" --data-urlencode 'exact=true' \
    "${KC_BASE}/users")
  user_id=$(jq -r '.[0].id // empty' <<<"${users}")
  if [[ -z "${user_id}" ]]; then
    body=$(jq -n --arg username "${username}" --arg email "${email}" \
      '{username:$username,enabled:true,email:$email,emailVerified:true,firstName:$username,lastName:"user"}')
    status=$(kc_admin_call -o /dev/null -w '%{http_code}' -X POST "${KC_BASE}/users" \
      -H 'Content-Type: application/json' --data "${body}")
    [[ "${status}" == 201 || "${status}" == 409 ]] || { echo "Keycloak user creation failed for ${username} (HTTP ${status})." >&2; return 1; }
    users=$(kc_admin_call -G --data-urlencode "username=${username}" --data-urlencode 'exact=true' \
      "${KC_BASE}/users")
    user_id=$(jq -r '.[0].id // empty' <<<"${users}")
    [[ -n "${user_id}" ]] || { echo "Could not find Keycloak user ${username} after creation." >&2; return 1; }
  fi

  jq -n --rawfile password "${password_file}" \
    '{type:"password",value:($password|rtrimstr("\n")),temporary:false}' \
    > "${WORK_DIR}/password-body.json"
  chmod 600 "${WORK_DIR}/password-body.json"
  status=$(kc_admin_call -o /dev/null -w '%{http_code}' -X PUT \
    "${KC_BASE}/users/${user_id}/reset-password" -H 'Content-Type: application/json' \
    --data-binary "@${WORK_DIR}/password-body.json")
  [[ "${status}" == 204 ]] || { echo "Keycloak password setup failed for ${username} (HTTP ${status})." >&2; return 1; }

  body=$(jq -n '{requiredActions:[]}')
  status=$(kc_admin_call -o /dev/null -w '%{http_code}' -X PUT \
    "${KC_BASE}/users/${user_id}" -H 'Content-Type: application/json' --data "${body}")
  [[ "${status}" == 204 ]] || { echo "Could not clear Keycloak required actions for ${username} (HTTP ${status})." >&2; return 1; }

  body=$(jq -n --arg id "${user_id}" '$id')
  status=$(kc_admin_call -o /dev/null -w '%{http_code}' -X POST \
    "${KC_BASE}/organizations/${org_id}/members" -H 'Content-Type: application/json' --data "${body}")
  if [[ "${status}" != 201 && "${status}" != 204 && "${status}" != 409 && "${status}" != 200 ]]; then
    status=$(kc_admin_call -o /dev/null -w '%{http_code}' -X PUT \
      "${KC_BASE}/organizations/${org_id}/members/${user_id}")
  fi
  [[ "${status}" == 201 || "${status}" == 204 || "${status}" == 409 || "${status}" == 200 ]] || {
    echo "Could not add ${username} to Keycloak organization (HTTP ${status})." >&2
    return 1
  }
  printf '%s\n' "${user_id}"
}

kc_ensure_user "${TENANT_ADMIN_USER}" "${WORK_DIR}/admin-password" "${TENANT_ADMIN_USER}@example.com" >/dev/null
kc_ensure_user "${TENANT_USER}" "${WORK_DIR}/user-password" "${TENANT_USER}@example.com" >/dev/null
# Both calls reset credentials, so report the onboarding run as changed.
changed=1

# JIT-provision OSAC user records using isolated config directories.
osac --config "${ADMIN_CONFIG}" login --insecure --address "${OSAC_PUBLIC_ADDRESS}" \
  --user "${TENANT_ADMIN_USER}" --password-file "${WORK_DIR}/admin-password" >/dev/null
osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" get projects >/dev/null 2>&1 || true
osac --config "${USER_CONFIG}" login --insecure --address "${OSAC_PUBLIC_ADDRESS}" \
  --user "${TENANT_USER}" --password-file "${WORK_DIR}/user-password" >/dev/null
osac --config "${USER_CONFIG}" --tenant "${OSAC_TENANT_NAME}" get projects >/dev/null 2>&1 || true

users=$(osac_tenant get users -o json | json_items)
user_osac_id() {
  local username="$1"
  jq -r --arg name "${username}" --arg tenant "${OSAC_TENANT_NAME}" '
    [.[] | select(.metadata.tenant == $tenant and .spec.username == $name)]
    | if length == 1 then .[0].id else error("Expected exactly one tenant user for " + $name) end
  ' <<<"${users}"
}

admin_osac_id=$(user_osac_id "${TENANT_ADMIN_USER}")
user_osac_id_value=$(user_osac_id "${TENANT_USER}")
[[ "${admin_osac_id}" =~ ^[0-9a-f-]{36}$ && "${user_osac_id_value}" =~ ^[0-9a-f-]{36}$ ]] || {
  echo "Could not resolve tenant user IDs after Keycloak login." >&2
  exit 1
}

rolebinding_name="${OSAC_TENANT_NAME}-admin-binding"
rolebindings=$(osac_tenant get rolebindings -o json)
if ! jq -e --arg name "${rolebinding_name}" --arg user_id "${admin_osac_id}" '
  (if type == "array" then . elif has("items") then (.items // []) else [.] end)
  | any(.[]; .metadata.name == $name
      and .spec.role.name == "tenant-admin"
      and ((.spec.users // []) | any(.[]; .id == $user_id)))
' >/dev/null <<<"${rolebindings}"; then
  if jq -e --arg name "${rolebinding_name}" '
    (if type == "array" then . elif has("items") then (.items // []) else [.] end)
    | any(.[]; .metadata.name == $name)
  ' >/dev/null <<<"${rolebindings}"; then
    osac_tenant delete rolebinding "${rolebinding_name}" >/dev/null
    for attempt in $(seq 1 60); do
      remaining=$(osac_tenant get rolebindings -o json)
      if ! jq -e --arg name "${rolebinding_name}" '
        (if type == "array" then . elif has("items") then (.items // []) else [.] end)
        | any(.[]; .metadata.name == $name)
      ' >/dev/null <<<"${remaining}"; then
        break
      fi
      [[ "${attempt}" -lt 60 ]] || { echo "Timed out deleting rolebinding ${rolebinding_name}" >&2; exit 1; }
      sleep 2
    done
  fi
  cat > "${WORK_DIR}/rolebinding.yaml" <<EOF
"@type": type.googleapis.com/osac.public.v1.RoleBinding
metadata:
  name: ${rolebinding_name}
  tenant: ${OSAC_TENANT_NAME}
spec:
  role:
    name: tenant-admin
  users:
    - id: ${admin_osac_id}
EOF
  osac_tenant create -f "${WORK_DIR}/rolebinding.yaml" >/dev/null
  changed=1
fi

for attempt in $(seq 1 30); do
  rolebindings=$(osac_tenant get rolebindings -o json | json_items)
  rolebinding_state=$(jq -r --arg name "${rolebinding_name}" --arg tenant "${OSAC_TENANT_NAME}" '
    .[] | select(.metadata.name == $name and .metadata.tenant == $tenant) | .status.state // empty
  ' <<<"${rolebindings}")
  [[ "${rolebinding_state}" == ROLE_BINDING_STATE_READY || "${rolebinding_state}" == READY ]] && break
  [[ "${rolebinding_state}" == ROLE_BINDING_STATE_FAILED || "${rolebinding_state}" == FAILED ]] && {
    echo "RoleBinding ${rolebinding_name} reached FAILED." >&2; exit 1;
  }
  [[ "${attempt}" -eq 30 ]] && { echo "RoleBinding ${rolebinding_name} did not become READY." >&2; exit 1; }
  sleep 2
done

# Re-login so the admin token has the tenant-admin role.
osac --config "${ADMIN_CONFIG}" logout >/dev/null 2>&1 || true
osac --config "${ADMIN_CONFIG}" login --insecure --address "${OSAC_PUBLIC_ADDRESS}" \
  --user "${TENANT_ADMIN_USER}" --password-file "${WORK_DIR}/admin-password" >/dev/null

projects=$(osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" get projects -o json | json_items)
project=$(jq -c --arg tenant "${OSAC_TENANT_NAME}" --arg name "${TENANT_PROJECT}" '
  [.[] | select(.metadata.tenant == $tenant and .metadata.name == $name)][0] // {}
' <<<"${projects}")
if jq -e '.metadata.deletion_timestamp // empty' >/dev/null <<<"${project}"; then
  echo "Project ${TENANT_PROJECT} is still deleting; wait for deletion to finish before setup." >&2
  exit 1
fi
if [[ "${project}" == '{}' ]]; then
  cat > "${WORK_DIR}/project.yaml" <<EOF
"@type": type.googleapis.com/osac.public.v1.Project
metadata:
  name: ${TENANT_PROJECT}
  tenant: ${OSAC_TENANT_NAME}
spec:
  title: Default Project
  description: Primary project for ${OSAC_TENANT_NAME}
EOF
  if osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" \
    create -f "${WORK_DIR}/project.yaml" >/dev/null 2>"${WORK_DIR}/project-create.err"; then
    changed=1
  elif grep -q 'AlreadyExists' "${WORK_DIR}/project-create.err"; then
    # A login can JIT-provision the default project after our initial list.
    # Treat that create race as success only after the tenant-scoped project appears.
    for attempt in $(seq 1 30); do
      projects=$(osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" get projects -o json | json_items)
      project=$(jq -c --arg tenant "${OSAC_TENANT_NAME}" --arg name "${TENANT_PROJECT}" \
        '[.[] | select(.metadata.tenant == $tenant and .metadata.name == $name)][0] // {}' <<<"${projects}")
      [[ "${project}" != '{}' ]] && break
      [[ "${attempt}" -lt 30 ]] || {
        echo "Project ${TENANT_PROJECT} already exists but is not visible in tenant ${OSAC_TENANT_NAME}." >&2
        exit 1
      }
      sleep 2
    done
  else
    cat "${WORK_DIR}/project-create.err" >&2
    exit 1
  fi
fi

membership_name="${TENANT_USER}-viewer"
memberships=$(osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" \
  get projectmemberships --filter "this.metadata.name == '${membership_name}'" -o json)
if ! jq -e --arg name "${membership_name}" --arg project "${TENANT_PROJECT}" \
  --arg user_id "${user_osac_id_value}" '
  (if type == "array" then . elif has("items") then (.items // []) else [.] end)
  | any(.[]; .metadata.name == $name and .metadata.project == $project
      and .spec.role == "PROJECT_MEMBERSHIP_ROLE_VIEWER"
      and ((.spec.users // []) | any(.[]; .id == $user_id)))
' >/dev/null <<<"${memberships}"; then
  if jq -e --arg name "${membership_name}" '
    (if type == "array" then . elif has("items") then (.items // []) else [.] end)
    | any(.[]; .metadata.name == $name)
  ' >/dev/null <<<"${memberships}"; then
    osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" delete projectmembership "${membership_name}" >/dev/null
    for attempt in $(seq 1 60); do
      remaining=$(osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" get projectmemberships -o json)
      if ! jq -e --arg name "${membership_name}" '
        (if type == "array" then . elif has("items") then (.items // []) else [.] end)
        | any(.[]; .metadata.name == $name)
      ' >/dev/null <<<"${remaining}"; then
        break
      fi
      [[ "${attempt}" -lt 60 ]] || { echo "Timed out deleting projectmembership ${membership_name}" >&2; exit 1; }
      sleep 2
    done
  fi
  cat > "${WORK_DIR}/membership.yaml" <<EOF
"@type": type.googleapis.com/osac.public.v1.ProjectMembership
metadata:
  name: ${membership_name}
  tenant: ${OSAC_TENANT_NAME}
  project: ${TENANT_PROJECT}
spec:
  role: PROJECT_MEMBERSHIP_ROLE_VIEWER
  users:
    - id: ${user_osac_id_value}
EOF
  osac --config "${ADMIN_CONFIG}" --tenant "${OSAC_TENANT_NAME}" create -f "${WORK_DIR}/membership.yaml" >/dev/null
  changed=1
fi

if [[ "${changed}" -eq 1 ]]; then
  echo "CHANGED: tenant ${OSAC_TENANT_NAME} and its users are ready."
else
  echo "OK: tenant ${OSAC_TENANT_NAME} and its users are already ready."
fi
