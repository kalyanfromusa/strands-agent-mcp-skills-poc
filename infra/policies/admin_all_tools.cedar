// Policy "admin_all_tools": full access for shop administrators.
//
// What it does: lets any member of the shop-admin Cognito group (the demo user alice) list and call
// every tool on the ShopAssist Gateway: all orders___ and support___ tools, including the ones that
// change data (orders___create_return, support___create_ticket).
//
// Where it fits: Terraform (modules/gateway) and boto3 (infra/boto3/steps/policy.py) upload this file
// into the AgentCore policy engine. The Gateway runs in ENFORCE mode, which means default-deny: a
// request is refused unless at least one policy permits it. This file is one of those permits.
// Note: this whole file, comments included, is the policy text sent to AWS, so editing a comment
// here also shows up as a change on the next deploy.
//
// How to read it:
//   principal = who is calling. AgentCore::OAuthUser is any caller with a valid login token.
//   action    = which tool. Leaving it open means "any tool".
//   resource  = which Gateway. "is AgentCore::Gateway" means any Gateway this engine is attached to.
//   when      = the extra condition. The token's fields arrive as "tags" on the principal.
//
// Members of the shop-admin Cognito group may call every tool on the gateway.
// cognito:groups arrives as a JSON-array string, e.g. ["shop-admin"], so match it with `like`.
permit(
  principal is AgentCore::OAuthUser,
  action,
  resource is AgentCore::Gateway
)
when {
  principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*\"shop-admin\"*"
};
