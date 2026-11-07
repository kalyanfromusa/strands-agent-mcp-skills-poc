# Agent workflow
1. Pick the first unchecked task in the current work queue, `.kiro/specs/prod-deploy/prod-deploy-tasks.md`
   (the original `.kiro/specs/strands-poc/tasks.md` is complete).
2. Implement it within the contracts in `design.md`.
3. Verify it (unit tests, `terraform validate`, local run, or deployed smoke test as the task says).
4. Tick it. Never commit secrets, `.deploy/`, state files or `dist/`.
5. Cost hygiene: everything deployed must be destroyable by `terraform destroy` (main → platform → bootstrap).
6. AWS: use the profile `personal` per command for the app's resources and deploys (account 438750401181).
   Never use it for the Claude session itself. Confirm before any destroy.
