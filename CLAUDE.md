# CLAUDE.md

This workspace is **Kiro-compatible**. All project instructions, specs and tasks live in `.kiro/`, the single source of truth for both Kiro and Claude Code. Don't duplicate rules here. Edit the `.kiro` files instead.

## Always-loaded project rules
@.kiro/steering/agent-workflow.md
@.kiro/steering/product.md
@.kiro/steering/tech.md
@.kiro/steering/structure.md
@.kiro/steering/ux-standards.md
@.kiro/steering/decisions.md

## Spec (read when working on a task)
- Requirements: `.kiro/specs/strands-poc/requirements.md`
- Design and contracts: `.kiro/specs/strands-poc/design.md`
- **Tasks (work queue):** `.kiro/specs/strands-poc/tasks.md`

## Claude Code specifics
- "Next task" / "continue" means: follow `agent-workflow.md`. Pick the first unchecked task in `tasks.md`, implement it, verify it, tick it.
- Use `uv run` for Python. Local reference dumps (Terraform provider schema, boto3 operation list) are in `.ref/` (git-ignored).
