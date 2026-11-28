#!/usr/bin/env bash
# build-components.sh - build the OSAC component images for a Netris E2E job
# in the background, then load them onto the OCP node.
#
#   build-components.sh start   launch "build" detached and return at once
#   build-components.sh build   build every image in the foreground
#   build-components.sh load    wait for the build, load the images onto the
#                               node and write the "overrides" step output
#   build-components.sh abort   after a failed job: stop a running build and
#                               print its log if "load" never did
#
# The build needs only podman and the OSAC commit, so a job starts it before
# the lab and OCP deploy and collects the result once the node exists.
#
# Fulfillment Service, OSAC Operator, the AAP execution environment and the
# Bare-metal Fulfillment Operator are always built from OSAC_REPO at
# OSAC_BRANCH. COMPONENTS_JSON_INPUT adds more; an entry whose imageKey names
# one of those four is dropped.
#
# Env:
#   COMPONENT_STATE_DIR     (all, required) directory for the files below
#   COMPONENTS_JSON_INPUT   (build, start) JSON array of extra components
#   OSAC_REPO, OSAC_BRANCH  (build, start) source of the four core images
#   GITHUB_RUN_ID           (build, start) image tag suffix
#   COMPONENT_PYTHON        (build) Python that creates the ansible-builder
#                           venv, default /usr/bin/python3
#   KUBECONFIG              (load) cluster that owns the node
#   GITHUB_OUTPUT           (load) step output file
#   COMPONENT_WAIT_SECONDS  (load) how long to wait for the build, default 1800
#   COMPONENT_POLL_SECONDS  (load) poll interval, default 10
#
# Files in COMPONENT_STATE_DIR:
#   build.log       output of "build"            build.pid  the detached builder
#   status          exit code of "build", written last
#   log.printed     marks that build.log already went to the job log
#   overrides.json  {"<ansible variable>": "<image tag>", ...}
#   images.txt      one image tag per line, in build order
set -euo pipefail

MODE="${1:-}"
: "${COMPONENT_STATE_DIR:?COMPONENT_STATE_DIR is required}"

# Ansible variable that carries the image built for a Helm value path.
var_name_for() {
  case "$1" in
    service.images.service) echo fulfillment_service_image ;;
    operator.image.repository) echo osac_operator_image ;;
    aap.bootstrap.image) echo osac_aap_image ;;
    bmf.image.repository) echo bmf_operator_image ;;
    *) return 1 ;;
  esac
}

# The four core components followed by the extra ones.
components_json() {
  local core
  core=$(jq -nc \
    --arg repo "${OSAC_REPO}" \
    --arg ref "${OSAC_BRANCH}" \
    '[
      {repo: $repo, ref: $ref, containerfile: "fulfillment-service/Containerfile", context: ".", imageKey: "service.images.service"},
      {repo: $repo, ref: $ref, containerfile: "osac-operator/Containerfile", context: ".", imageKey: "operator.image.repository"},
      {repo: $repo, ref: $ref, containerfile: "osac-aap/execution-environment/execution-environment.yaml", imageKey: "aap.bootstrap.image", buildType: "ansible-builder"},
      {repo: $repo, ref: $ref, containerfile: "bare-metal-fulfillment-operator/Containerfile", context: ".", imageKey: "bmf.image.repository"}
    ]')
  local input="${COMPONENTS_JSON_INPUT:-}"
  if [[ -z "${input}" || "${input}" == "[]" ]]; then
    echo "${core}"
    return
  fi
  jq -c --argjson core "${core}" '
    map(select(
      .imageKey != "service.images.service" and
      .imageKey != "operator.image.repository" and
      .imageKey != "aap.bootstrap.image" and
      .imageKey != "aap.configAsCode.eeImage" and
      .imageKey != "bmf.image.repository"
    )) as $additional
    | $core + $additional
  ' <<<"${input}"
}

# ansible-builder lives in its own venv: the lab's ansible-playbook runs from
# the system Python at the same time, and a pip install there would replace
# jinja2, PyYAML and cryptography underneath it.
ansible_builder() {
  local venv="${COMPONENT_STATE_DIR}/ansible-builder-venv"
  if [[ ! -x "${venv}/bin/ansible-builder" ]]; then
    "${COMPONENT_PYTHON:-/usr/bin/python3}" -m venv "${venv}"
    "${venv}/bin/pip" install --quiet 'ansible-builder~=3.0'
  fi
  "${venv}/bin/ansible-builder" "$@"
}

build() {
  : "${OSAC_REPO:?OSAC_REPO is required}"
  : "${OSAC_BRANCH:?OSAC_BRANCH is required}"
  : "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
  mkdir -p "${COMPONENT_STATE_DIR}/src"

  local components count i
  components=$(components_json)
  count=$(jq 'length' <<<"${components}")

  # Reject an unknown imageKey before the first build, not after an hour of them.
  local image_key
  for i in $(seq 0 $((count - 1))); do
    image_key=$(jq -r ".[$i].imageKey" <<<"${components}")
    if ! var_name_for "${image_key}" > /dev/null; then
      echo "::error::components[$i].imageKey '${image_key}' is not a recognized override key"
      exit 1
    fi
  done

  local overrides="{}" images=() component_sha=()
  declare -A pinned_sha
  local repo ref file build_type context tag src repo_url sha effective_context
  local component_dir build_context containerfile_name var_name

  # Resolve every source first and validate all Containerfile contexts before
  # starting any expensive image build.  The second loop builds the exact
  # commits inspected here, even if a branch advances in the meantime.
  for i in $(seq 0 $((count - 1))); do
    repo=$(jq -r ".[$i].repo" <<<"${components}")
    ref=$(jq -r ".[$i].ref" <<<"${components}")
    file=$(jq -r ".[$i].containerfile // \"Containerfile\"" <<<"${components}")
    build_type=$(jq -r ".[$i].buildType // \"containerfile\"" <<<"${components}")
    context=$(jq -r ".[$i].context // empty" <<<"${components}")

    src="${COMPONENT_STATE_DIR}/src/component-${i}"

    echo "::group::Validate component[$i]: ${repo}@${ref} (${file})"
    repo_url="${repo}"
    if [[ "${repo_url}" != *://* && "${repo_url}" != git@* ]]; then
      repo_url="https://github.com/${repo_url%.git}.git"
    fi
    # Every component of one repo is built from the commit its first clone resolved.
    git init "${src}"
    if [[ -n "${pinned_sha[$repo]:-}" ]]; then
      sha="${pinned_sha[$repo]}"
      echo "Cloning ${repo} at pinned SHA ${sha} (first clone resolved this from ref '${ref}')."
      git -C "${src}" fetch --depth 1 "${repo_url}" "${sha}"
      git -C "${src}" checkout FETCH_HEAD
    else
      git -C "${src}" fetch --depth 1 "${repo_url}" "${ref}"
      git -C "${src}" checkout FETCH_HEAD
      sha=$(git -C "${src}" rev-parse HEAD)
      pinned_sha[$repo]="${sha}"
      echo "Pinned ${repo} to SHA ${sha} (resolved from ref '${ref}')."
    fi
    component_sha[$i]="${sha}"

    if [[ "${build_type}" != "ansible-builder" ]]; then
      effective_context="${context:-$(dirname "${file}")}"
      python3 .github/scripts/validate_containerfile_context.py \
        --label "components[$i]" \
        --repo-root "${src}" \
        --containerfile "${file}" \
        --context "${effective_context}"
    fi
    echo "::endgroup::"
  done

  for i in $(seq 0 $((count - 1))); do
    repo=$(jq -r ".[$i].repo" <<<"${components}")
    ref=$(jq -r ".[$i].ref" <<<"${components}")
    file=$(jq -r ".[$i].containerfile // \"Containerfile\"" <<<"${components}")
    image_key=$(jq -r ".[$i].imageKey" <<<"${components}")
    build_type=$(jq -r ".[$i].buildType // \"containerfile\"" <<<"${components}")
    context=$(jq -r ".[$i].context // empty" <<<"${components}")

    tag="localhost/component-${i}:${GITHUB_RUN_ID}"
    src="${COMPONENT_STATE_DIR}/src/component-${i}"
    sha="${component_sha[$i]}"

    echo "::group::Build component[$i]: ${repo}@${ref} (${file})"

    component_dir="${src}"
    containerfile_name="${file}"
    if [[ "${file}" == */* ]]; then
      component_dir="${src}/$(dirname "${file}")"
      containerfile_name="$(basename "${file}")"
    fi
    if [[ -n "${context}" ]]; then
      build_context="${src}/${context}"
    else
      build_context="${component_dir}"
    fi

    if [[ "${build_type}" == "ansible-builder" ]]; then
      ansible_builder create --file "${component_dir}/${containerfile_name}" --context "${component_dir}/context"
      podman build -t "${tag}" -f "${component_dir}/context/Containerfile" "${component_dir}/context"
    else
      podman build --env GODEBUG=http2client=0 --unsetenv GODEBUG -t "${tag}" -f "${component_dir}/${containerfile_name}" "${build_context}"
    fi

    var_name=$(var_name_for "${image_key}")
    overrides=$(jq -c --arg k "${var_name}" --arg v "${tag}" '. + {($k): $v}' <<<"${overrides}")
    images+=("${tag}")
    echo "Built ${var_name}=${tag}"
    echo "::endgroup::"
  done

  # Written only after the last build, so a reader never sees a partial result.
  printf '%s\n' "${images[@]}" > "${COMPONENT_STATE_DIR}/images.txt.tmp"
  mv "${COMPONENT_STATE_DIR}/images.txt.tmp" "${COMPONENT_STATE_DIR}/images.txt"
  echo "${overrides}" > "${COMPONENT_STATE_DIR}/overrides.json.tmp"
  mv "${COMPONENT_STATE_DIR}/overrides.json.tmp" "${COMPONENT_STATE_DIR}/overrides.json"
}

# Launches "build" in its own session so it outlives the workflow step that
# started it. The exit code is recorded last, by rename, so "status" is either
# absent or complete.
start() {
  mkdir -p "${COMPONENT_STATE_DIR}"
  rm -f "${COMPONENT_STATE_DIR}/status" "${COMPONENT_STATE_DIR}/build.pid"
  # shellcheck disable=SC2016  # expanded by the detached shell, not here
  setsid bash -c '
    echo "$$" > "$2/build.pid"
    rc=0
    "$1" build > "$2/build.log" 2>&1 || rc=$?
    echo "${rc}" > "$2/status.tmp"
    mv "$2/status.tmp" "$2/status"
  ' build-components "$(realpath "$0")" "${COMPONENT_STATE_DIR}" < /dev/null > /dev/null 2>&1 &
  for _ in $(seq 1 50); do
    [[ -s "${COMPONENT_STATE_DIR}/build.pid" ]] && break
    sleep 0.1
  done
  [[ -s "${COMPONENT_STATE_DIR}/build.pid" ]] || {
    echo "::error::The component image build did not start"
    exit 1
  }
  echo "Component image build started (pid $(cat "${COMPONENT_STATE_DIR}/build.pid")); log: ${COMPONENT_STATE_DIR}/build.log"
}

print_build_log() {
  if [[ -f "${COMPONENT_STATE_DIR}/build.log" ]]; then
    cat "${COMPONENT_STATE_DIR}/build.log"
    : > "${COMPONENT_STATE_DIR}/log.printed"
  fi
}

builder_alive() {
  local pid
  pid=$(cat "${COMPONENT_STATE_DIR}/build.pid" 2> /dev/null) || return 1
  [[ -n "${pid}" ]] && kill -0 "${pid}" 2> /dev/null
}

load() {
  : "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
  local wait_seconds="${COMPONENT_WAIT_SECONDS:-1800}"
  local poll_seconds="${COMPONENT_POLL_SECONDS:-10}"
  local status_file="${COMPONENT_STATE_DIR}/status"
  local deadline=$((SECONDS + wait_seconds))

  [[ -f "${status_file}" ]] || echo "Waiting up to ${wait_seconds} seconds for the component image build..."
  until [[ -f "${status_file}" ]]; do
    if ! builder_alive; then
      # The builder writes its status just before it exits: look once more.
      [[ -f "${status_file}" ]] && break
      print_build_log
      echo "::error::The component image build is not running and recorded no result"
      exit 1
    fi
    if ((SECONDS >= deadline)); then
      print_build_log
      echo "::error::The component image build did not finish within ${wait_seconds} seconds"
      exit 1
    fi
    sleep "${poll_seconds}"
  done

  print_build_log
  local status
  status=$(cat "${status_file}")
  if [[ "${status}" != "0" ]]; then
    echo "::error::The component image build failed with exit code ${status}"
    exit 1
  fi

  local node tag
  node=$(oc get nodes -o jsonpath='{.items[0].metadata.name}')
  while IFS= read -r tag; do
    echo "Loading ${tag} onto node ${node}..."
    podman save "${tag}" | oc debug "node/${node}" -t=false -- chroot /host podman load
  done < "${COMPONENT_STATE_DIR}/images.txt"

  echo "overrides=$(jq -c . "${COMPONENT_STATE_DIR}/overrides.json")" >> "${GITHUB_OUTPUT}"
}

# For a job that failed or was cancelled: stops a build that is still running
# and prints its log unless "load" already did, so the output of a build that
# never reached "load" is not lost with the box. Never fails the step.
abort() {
  local pid
  if builder_alive; then
    pid=$(cat "${COMPONENT_STATE_DIR}/build.pid")
    echo "Stopping the component image build (process group ${pid})..."
    # "start" made the builder a session leader, so its PID names its group.
    kill -TERM -- "-${pid}" 2> /dev/null || true
  fi
  if [[ ! -e "${COMPONENT_STATE_DIR}/log.printed" ]]; then
    print_build_log
    echo "::endgroup::"
  fi
}

case "${MODE}" in
  build) build ;;
  start) start ;;
  load) load ;;
  abort) abort ;;
  *)
    echo "usage: $0 start|build|load|abort" >&2
    exit 2
    ;;
esac
