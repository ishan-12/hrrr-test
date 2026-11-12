# Skill sources

Vendored copies of third-party agent skills, pinned to the commit they were taken from.
All three source repositories are MIT-licensed.

| Skill | Source | Commit |
|---|---|---|
| grill-me, grilling | [mattpocock/skills](https://github.com/mattpocock/skills) `skills/productivity/` | 4ae2317ddb22 |
| codebase-design, tdd, research, retro | [mattpocock/skills](https://github.com/mattpocock/skills) `skills/engineering/` | 4ae2317ddb22 |
| writing-for-agents (needed by retro) | [mattpocock/skills](https://github.com/mattpocock/skills) `skills/productivity/` | 4ae2317ddb22 |
| systematic-debugging | [obra/superpowers](https://github.com/obra/superpowers) `skills/` | 22f0b463e7a5 |
| documentation-and-adrs | [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) `skills/` | 301b02b12bff |

## Local changes

- `systematic-debugging/SKILL.md`: the reference to `superpowers:test-driven-development` now points to the
  local `tdd` skill. The reference to `superpowers:verification-before-completion` now points to
  llm-rule-engine R9, which enforces the same thing with recorded evidence.
- `systematic-debugging/root-cause-tracing.md`: the npm-only `find-polluter.sh` script was dropped and
  replaced with a one-line pytest loop.
- The `agents/openai.yaml` files (Codex metadata) were not copied.
