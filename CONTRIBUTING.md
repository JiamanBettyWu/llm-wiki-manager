# Local canonical checkout

For this machine, `/Users/jiamanwu/.agents/skills/llm-wiki-manager` is the canonical git checkout for this skill. Make changes, run validation, commit, and push only here.

Claude consumes this same checkout through the symlink at `/Users/jiamanwu/.claude/skills/llm-wiki-manager`; it must never be a second clone. Other agents should similarly point their skill installation at this checkout rather than maintain copies.

Keep `AGENTS.md` as the wiki schema. `CLAUDE.md` is a compatibility stub that imports `AGENTS.md`; do not create a second schema there.
