#!/usr/bin/env bash
# =====================================================================================================
# scripts/ci_image_checks.sh: start each freshly built image and prove it works, before anything is pushed
#
#   scripts/ci_image_checks.sh agent         <image ref>
#   scripts/ci_image_checks.sh support_mcp   <image ref>
#   scripts/ci_image_checks.sh orders_lambda <image ref>
#   scripts/ci_image_checks.sh role_mapper   <image ref>
#
# What this file does
#   Runs one container image locally on the CI runner (no AWS access, in-memory demo data) and calls it
#   the way AWS would:
#     agent         GET  /ping on port 8080 must answer 200 (the AgentCore Runtime health check).
#     support_mcp   POST /mcp `initialize` then `tools/list` must list create_ticket, get_ticket, search_faq.
#     orders_lambda the Lambda Runtime Interface Emulator (built into the base image) is invoked as the
#                   Gateway would for `get_order` 1042, and must return that order.
#   Exits non-zero (and prints the container logs) if any check fails.
#
# Where it fits
#   .github/workflows/ci.yml, build.yml and deploy.yml run it on the native arm64 runner right after
#   `docker buildx build --load`. deploy.yml only pushes images that passed. Images are never built on a
#   developer machine (decision 2026-11-07), so this is the "does it start?" safety net.
#
# Step by step
#   1. Start the container in the background, with demo settings (STORE=memory, data/seed.json mounted).
#   2. Wait until it answers (up to 60 seconds).
#   3. Make the image-specific call and check the answer.
#   4. Always stop and remove the container, even when a check fails.
#
# Key terms
#   RIE (Runtime Interface Emulator): a small server in AWS's Lambda images that accepts invocations on
#     port 8080 inside the container, so a Lambda image can be tested without AWS.
#   MCP initialize: the handshake an MCP client sends before listing or calling tools.
# =====================================================================================================
set -euo pipefail

kind="${1:?usage: ci_image_checks.sh agent|support_mcp|orders_lambda <image>}"
image="${2:?usage: ci_image_checks.sh agent|support_mcp|orders_lambda <image>}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
name="check-${kind}-$$"

# Step 4 (registered first): clean up whatever happens.
cleanup() {
  if [ "${failed:-0}" = 1 ]; then
    echo "---- container logs (${name}) ----"
    docker logs "${name}" 2>&1 | tail -50 || true
  fi
  docker rm -f "${name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { failed=1; echo "FAIL ${kind}: $*" >&2; exit 1; }

# Step 2 helper: retry a command until it succeeds or 60 s pass.
wait_for() {
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

seed_mount=(-v "${repo_root}/data/seed.json:/seed/seed.json:ro" -e SEED_FILE=/seed/seed.json -e STORE=memory)

case "${kind}" in
  agent)
    # Step 1: no Gateway is reachable here; /ping doesn't need one (the agent connects lazily).
    docker run -d --name "${name}" -p 18080:8080 -e GATEWAY_AUTH_MODE=none -e AWS_REGION=us-east-1 "${image}" >/dev/null
    # Steps 2-3: the Runtime health check.
    wait_for curl -fsS http://127.0.0.1:18080/ping || fail "/ping did not answer 200"
    echo "OK agent: /ping -> $(curl -fsS http://127.0.0.1:18080/ping)"
    ;;

  support_mcp)
    docker run -d --name "${name}" -p 18000:8000 "${seed_mount[@]}" "${image}" >/dev/null
    hdr=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
    init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ci","version":"1"}}}'
    # Step 2: wait until the MCP endpoint accepts the handshake.
    wait_for curl -fsS "${hdr[@]}" -d "${init}" http://127.0.0.1:18000/mcp || fail "MCP initialize failed"
    # Step 3: list the tools (stateless server: no session id needed).
    tools="$(curl -fsS "${hdr[@]}" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' http://127.0.0.1:18000/mcp)"
    for t in create_ticket get_ticket search_faq; do
      grep -q "\"${t}\"" <<<"${tools}" || fail "tools/list is missing ${t}"
    done
    echo "OK support_mcp: tools/list has create_ticket, get_ticket, search_faq"
    ;;

  orders_lambda)
    docker run -d --name "${name}" -p 19000:8080 "${seed_mount[@]}" "${image}" >/dev/null
    url=http://127.0.0.1:19000/2015-03-31/functions/function/invocations
    # The Gateway passes the tool name in the Lambda client context (base64 JSON).
    ctx="$(printf '{"custom":{"bedrockAgentCoreToolName":"orders___get_order"}}' | base64 | tr -d '\n')"
    wait_for curl -fsS -d '{}' "${url}" || fail "the Lambda emulator did not start"
    out="$(curl -fsS -H "X-Amz-Client-Context: ${ctx}" -d '{"order_id":"1042"}' "${url}")"
    grep -q '"order_id": *"1042"' <<<"${out}" || fail "get_order 1042 returned: ${out}"
    echo "OK orders_lambda: get_order 1042 -> $(cut -c1-120 <<<"${out}")"
    ;;

  role_mapper)
    docker run -d --name "${name}" -p 19001:8080 \
      -e ENTRA_PROVIDER_NAME=EntraID -e ENTRA_ROLE_SOURCE=roles \
      -e 'ENTRA_ROLE_MAP={"AppRole-ShopAssist-Admin":"Group-ShopAssist-Admin"}' "${image}" >/dev/null
    url=http://127.0.0.1:19001/2015-03-31/functions/function/invocations
    wait_for curl -fsS -d '{}' "${url}" || fail "the Lambda emulator did not start"
    ev='{"version":"1","triggerSource":"TokenGeneration_HostedAuth","request":{"userAttributes":{"sub":"u1","identities":"[{\"providerName\":\"EntraID\"}]","custom:entra_roles":"[\"AppRole-ShopAssist-Admin\"]"}},"response":{}}'
    out="$(curl -fsS -d "${ev}" "${url}")"
    grep -q '"groupsToOverride": *\["Group-ShopAssist-Admin"\]' <<<"${out}" || fail "role mapping returned: ${out}"
    echo "OK role_mapper: AppRole-ShopAssist-Admin -> Group-ShopAssist-Admin"
    ;;

  *)
    fail "unknown image kind (agent | support_mcp | orders_lambda | role_mapper)"
    ;;
esac
