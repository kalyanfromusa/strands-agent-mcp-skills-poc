// Policy "viewer_read_only": look-but-don't-change access for shop viewers.
//
// What it does: lets members of the shop-viewer Cognito group (the demo user bob) list and call only
// the five tools that read data: get_order, list_customer_orders, check_return_eligibility,
// get_ticket and search_faq. The tools that change data (create_return in the orders target and
// create_ticket in the support target) are not in the list, so bob does not even see them in
// tools/list. (Keep their full prefixed names out of this file: a test checks they never appear here.)
//
// Where it fits: uploaded into the AgentCore policy engine by Terraform (modules/gateway) and boto3
// (infra/boto3/steps/policy.py). Before upload, the Gateway ARN placeholder below is replaced with the
// real Gateway ARN (Amazon Resource Name, its unique id), so this permit only applies to our Gateway.
// Note: this whole file, comments included, is the policy text sent to AWS, so editing a comment
// here also shows up as a change on the next deploy.
//
// Why default-deny matters: the Gateway runs in ENFORCE mode, where anything not permitted is refused.
// Because this policy lists what is allowed (instead of forbidding what is not), a tool added later is
// automatically hidden from viewers until someone adds it here on purpose.
// Users with no group (the demo user carol) match no policy at all; the agent runtime already turns
// them away before they reach the Gateway.
//
// How to read it:
//   principal = any caller with a valid login token (AgentCore::OAuthUser).
//   action    = one of the listed tools. Names are <target>___<tool> (three underscores).
//   resource  = exactly our Gateway.
//   when      = only if the token's cognito:groups field contains "shop-viewer".
//
// Members of the shop-viewer group may only use read-only tools.
// Listing the allowed actions (rather than forbidding the write ones) means any new tool is denied by default.
permit(
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"orders___get_order",
    AgentCore::Action::"orders___list_customer_orders",
    AgentCore::Action::"orders___check_return_eligibility",
    AgentCore::Action::"support___get_ticket",
    AgentCore::Action::"support___search_faq"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
)
when {
  principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*\"shop-viewer\"*"
};
