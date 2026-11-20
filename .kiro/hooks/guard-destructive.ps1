$in = [Console]::In.ReadToEnd()
$pattern = 'terraform\s+(destroy|apply|import|state\s+rm)|aws\s.*\b(delete|terminate|remove|destroy)|rm\s+-rf|Remove-Item.*-Recurse|git\s+push|git\s+reset\s+--hard|git\s+clean|destroy_everything'
if ($in -match $pattern) {
    @{ hookSpecificOutput = @{ permissionDecision = 'ask'; permissionDecisionReason = 'Destructive, deploy or push command. Confirm before running.' } } | ConvertTo-Json -Compress
}
exit 0
