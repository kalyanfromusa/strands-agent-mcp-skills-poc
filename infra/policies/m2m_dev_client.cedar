// Policy "m2m_dev_client": full access for the machine-to-machine (M2M) test client.
//
// What it does: lets the Cognito M2M app client (a program logging in as itself with a client id and
// secret, no person involved) list and call every tool. Developers and automated smoke tests use it to
// talk to the Gateway directly, without going through the agent.
//
// Where it fits: uploaded into the AgentCore policy engine by Terraform (modules/gateway). Before upload, the client id placeholder in the condition below is
// replaced with the real M2M client id. Under the Gateway's default-deny (ENFORCE) mode, without this
// permit the M2M client could log in but would see no tools at all.
// Note: this whole file, comments included, is the policy text sent to AWS, so editing a comment
// here also shows up as a change on the next deploy.
//
// How to read it:
//   principal = any caller with a valid login token (AgentCore::OAuthUser).
//   action    = any tool. resource = any Gateway this engine is attached to.
//   when      = only if the token's client_id field equals the M2M client's id.
//
// The machine-to-machine client (local development and automated smoke tests) may call every tool.
// M2M tokens carry no user groups, so match the client_id claim instead.
permit(
  principal is AgentCore::OAuthUser,
  action,
  resource is AgentCore::Gateway
)
when {
  principal.hasTag("client_id") &&
  principal.getTag("client_id") == "{{M2M_CLIENT_ID}}"
};
