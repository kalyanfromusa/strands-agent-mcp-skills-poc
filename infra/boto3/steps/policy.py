"""AgentCore Policy: a Cedar policy engine attached to the Gateway decides which tools each caller may use.

What this file does
    Two deploy steps. Step 4 creates the policy engine (an empty rulebook). Step 10 loads the rules
    (Cedar policies) into it, such as "shop-admin may call every tool" and "shop-viewer may only read".

Where it fits
    The engine must exist before the IAM roles (step 5, the Gateway role is limited to this engine) and
    before the Gateway (step 9, which attaches it). The rules come after the Gateway targets, because AWS
    checks each rule against the Gateway's real tool names. At run time the Gateway asks the engine about
    every tool call and every tool list.

* **Policy engine** `<prefix>_policy_engine` is a container for Cedar policies. It is created *before*
  the Gateway, which references it in `policyEngineConfiguration {arn, mode: ENFORCE}`. In ENFORCE mode
  everything is denied unless a policy permits it, and `tools/list` is filtered per principal, so a
  viewer never even sees `create_return`.
* **Policies** come from the shared templates in `infra/policies/*.cedar` (also used by Terraform),
  with `{{GATEWAY_ARN}}` and `{{M2M_CLIENT_ID}}` substituted. They are created *after* the targets
  exist, because CreatePolicy validates the actions named in the statement (`orders___get_order`, ...)
  against the Gateway's tools. A policy that fails validation ends in CREATE_FAILED with
  `statusReasons`, which we print.

Principals are `AgentCore::OAuthUser`; JWT claims become string tags (arrays as JSON text, hence `like`).

Step by step
    deploy_engine (step 4): find or create the engine, wait until ACTIVE, save its ID and ARN.
    deploy_policies (step 10):
        1. Read every .cedar file and fill in the Gateway ARN and M2M client ID.
        2. For each: recreate it if FAILED, create it if missing, update it if the text changed.
        3. Wait until each policy is ACTIVE.
        4. Delete policies in AWS that no longer have a .cedar file.

Key terms
    Cedar: an open-source language for writing access rules ("permit ... when ...").
    Principal: who is asking (here a logged-in user or the M2M client).
    Action: what they want to do (here: call one tool, e.g. `orders___create_return`).
    Default deny: anything not explicitly permitted is refused.
"""

from __future__ import annotations

from common import POLICIES_DIR, Context, OK, ignore_not_found, info, is_not_found, paginate, wait_for_status

# Statuses that mean something went wrong, so waiting stops with an error.
ENGINE_FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
POLICY_FAILED = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}


# ------------------------------------------------------------------ request builders (pure)


def build_create_engine_request(name: str, tags: dict[str, str]) -> dict:
    """CreatePolicyEngine request: just a name and description; the rules are added later."""
    return {"name": name, "tags": tags,
            "description": "Cedar policies deciding which caller may list and call which ShopAssist tool"}


def render_policy(template: str, gateway_arn: str, m2m_client_id: str) -> str:
    """Fill the placeholders in a .cedar template with this deployment's real values."""
    return template.replace("{{GATEWAY_ARN}}", gateway_arn).replace("{{M2M_CLIENT_ID}}", m2m_client_id).strip()


def load_policies(gateway_arn: str, m2m_client_id: str) -> dict[str, str]:
    """policy name (file stem, valid as [A-Za-z][A-Za-z0-9_]*) -> rendered Cedar statement."""
    return {p.stem: render_policy(p.read_text(encoding="utf-8"), gateway_arn, m2m_client_id)
            for p in sorted(POLICIES_DIR.glob("*.cedar"))}


def build_create_policy_request(engine_id: str, name: str, statement: str) -> dict:
    """CreatePolicy request.

    validationMode FAIL_ON_ANY_FINDINGS: AWS checks the rule against the Gateway's tools and refuses it
    if anything is wrong (e.g. a misspelt tool name), instead of silently storing a rule that never matches.
    """
    return {
        "policyEngineId": engine_id,
        "name": name,
        "description": f"ShopAssist policy from infra/policies/{name}.cedar",
        "definition": {"cedar": {"statement": statement}},
        "validationMode": "FAIL_ON_ANY_FINDINGS",
    }


def build_update_policy_request(engine_id: str, policy_id: str, statement: str) -> dict:
    """UpdatePolicy request: replace the rule text, with the same strict validation."""
    return {"policyEngineId": engine_id, "policyId": policy_id,
            "definition": {"cedar": {"statement": statement}}, "validationMode": "FAIL_ON_ANY_FINDINGS"}


# ------------------------------------------------------------------ helpers


def find_engine(ac, name: str) -> dict | None:
    """Find our policy engine by name (AWS API ListPolicyEngines). None if it does not exist."""
    for engine in paginate(ac.list_policy_engines, "policyEngines"):
        if engine["name"] == name:
            return engine
    return None


def _engine_status(ac, engine_id: str):
    """(status, reasons) of the engine via AWS API GetPolicyEngine; (None, "") once deleted."""
    try:
        e = ac.get_policy_engine(policyEngineId=engine_id)
        return e["status"], "; ".join(e.get("statusReasons", []))
    except Exception as err:  # noqa: BLE001
        if is_not_found(err):
            return None, ""
        raise


def _policy_status(ac, engine_id: str, policy_id: str):
    """(status, reasons) of one policy via AWS API GetPolicy; (None, "") once deleted."""
    try:
        p = ac.get_policy(policyEngineId=engine_id, policyId=policy_id)
        return p["status"], "; ".join(p.get("statusReasons", []))
    except Exception as err:  # noqa: BLE001
        if is_not_found(err):
            return None, ""
        raise


# ------------------------------------------------------------------ deploy: engine (before the gateway)


def deploy_engine(ctx: Context) -> None:
    """Deploy step 4: find or create the policy engine, wait until ACTIVE, and save its ID and ARN."""
    ac = ctx.client("bedrock-agentcore-control")
    name = ctx.names.policy_engine
    engine = find_engine(ac, name)
    if engine is None:
        # AWS API CreatePolicyEngine.
        engine = ac.create_policy_engine(**build_create_engine_request(name, ctx.names.tags))
        info(f"{OK} create requested: policy engine {engine['policyEngineId']}")
    else:
        info(f"policy engine {name} exists ({engine['policyEngineId']})")
    engine_id = engine["policyEngineId"]
    wait_for_status(f"policy engine {name}", lambda: _engine_status(ac, engine_id),
                    ready={"ACTIVE"}, failed=ENGINE_FAILED | {"DELETED"}, timeout=300)
    ctx.state.put("policy", engine_id=engine_id, engine_arn=engine["policyEngineArn"])


# ------------------------------------------------------------------ deploy: policies (after the targets)


def deploy_policies(ctx: Context) -> None:
    """Deploy step 10: make the policies in AWS match the .cedar files exactly."""
    ac = ctx.client("bedrock-agentcore-control")
    engine_id = ctx.state.section("policy")["engine_id"]
    # Step 1: the desired rules (from the files) and the current ones (AWS API ListPolicies).
    desired = load_policies(ctx.state.section("gateway")["arn"], ctx.state.section("cognito")["m2m_client_id"])
    existing = {p["name"]: p for p in paginate(ac.list_policies, "policies", policyEngineId=engine_id)}

    policy_ids = {}
    for name, statement in desired.items():
        current = existing.get(name)
        # Step 2a: a failed policy can't be repaired, so delete it (DeletePolicy), wait, and start again.
        if current and current["status"] in POLICY_FAILED:
            info(f"policy {name} is {current['status']}: {'; '.join(current.get('statusReasons', []))}; recreating")
            ac.delete_policy(policyEngineId=engine_id, policyId=current["policyId"])
            wait_for_status(f"policy {name} deletion", lambda: _policy_status(ac, engine_id, current["policyId"]),
                            ready={"DELETED"}, timeout=120, interval=2)
            current = None
        if current is None:
            # Step 2b: missing -> AWS API CreatePolicy.
            policy_id = ac.create_policy(**build_create_policy_request(engine_id, name, statement))["policyId"]
            info(f"{OK} create requested: policy {name}")
        else:
            # Step 2c: present -> compare the rule text and AWS API UpdatePolicy only if it changed.
            policy_id = current["policyId"]
            current_stmt = current.get("definition", {}).get("cedar", {}).get("statement", "").strip()
            if current_stmt != statement:
                ac.update_policy(**build_update_policy_request(engine_id, policy_id, statement))
                info(f"{OK} update requested: policy {name}")
            else:
                info(f"policy {name} exists and matches")
        # Step 3: wait until AWS has validated and activated the rule (a bad rule ends as CREATE_FAILED).
        wait_for_status(f"policy {name}", lambda: _policy_status(ac, engine_id, policy_id),
                        ready={"ACTIVE"}, failed=POLICY_FAILED | {"DELETED"}, timeout=180, interval=3)
        policy_ids[name] = policy_id
        ctx.state.put("policy", policies=policy_ids)

    # Step 4: clean-up.
    # Policies that no longer have a .cedar file are removed, so the files stay the source of truth.
    for name, p in existing.items():
        if name not in desired:
            ac.delete_policy(policyEngineId=engine_id, policyId=p["policyId"])
            info(f"{OK} deleted stale policy {name}")


# ------------------------------------------------------------------ destroy


def destroy_policies(ctx: Context) -> None:
    """Delete every policy in the engine, one by one, waiting for each (the first destroy step)."""
    ac = ctx.client("bedrock-agentcore-control")
    engine = find_engine(ac, ctx.names.policy_engine)
    if engine is None:
        info("policy engine already gone (no policies to delete)")
        return
    engine_id = engine["policyEngineId"]
    for p in list(paginate(ac.list_policies, "policies", policyEngineId=engine_id)):
        ignore_not_found(ac.delete_policy, policyEngineId=engine_id, policyId=p["policyId"])
        wait_for_status(f"policy {p['name']} deletion", lambda: _policy_status(ac, engine_id, p["policyId"]),
                        ready={"DELETED"}, failed={"DELETE_FAILED"}, timeout=120, interval=2)
    ctx.state.section("policy").pop("policies", None)
    ctx.state.save()


def destroy_engine(ctx: Context) -> None:
    """Runs after the gateway is gone (an engine still attached to a gateway can't be deleted)."""
    ac = ctx.client("bedrock-agentcore-control")
    engine = find_engine(ac, ctx.names.policy_engine)
    if engine is None:
        info("policy engine already gone")
    else:
        # AWS API DeletePolicyEngine, then wait until it is gone.
        engine_id = engine["policyEngineId"]
        ignore_not_found(ac.delete_policy_engine, policyEngineId=engine_id)
        wait_for_status(f"policy engine {engine_id} deletion", lambda: _engine_status(ac, engine_id),
                        ready={"DELETED"}, failed={"DELETE_FAILED"}, timeout=300, interval=3)
    ctx.state.drop("policy")
