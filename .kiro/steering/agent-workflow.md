# Agent workflow
1. Pick the first unchecked task in `.kiro/specs/strands-poc/tasks.md`.
2. Implement it within the contracts in `design.md`.
3. Verify it (unit tests, `terraform validate`, local run, or deployed smoke test as the task says).
4. Tick it. Never commit secrets, `.deploy/`, state files or `dist/`.
5. Cost hygiene: anything deployed for testing must be destroyable by `terraform destroy` or `infra/boto3/destroy.py`.
