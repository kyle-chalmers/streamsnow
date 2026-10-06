# Running these skills outside Claude Code

These skills are written for Claude Code and follow the open Agent Skills format (a folder with a
`SKILL.md`), so an agent that reads that format can follow them too; OpenAI Codex CLI is the one
tested (`streamsnow agent-skills install --agent codex` puts them where it looks). The `streamsnow`
CLI, pre-commit and CI do the enforcing and work the same under any agent. Read the skill text
with these translations:

- **Skill names.** `/build-app`, `/review-app <slug>` and the rest are Claude Code's syntax. When a
  step names the next skill for the user, give your agent's syntax instead (Codex: `$ship-app
  <slug>`, or pick it from `/skills`). When a step says to follow another skill's instructions, read
  that skill's `SKILL.md` (a sibling folder of this one) and do what it says.
- **Checkpoints.** A checkpoint is a plain question: ask it, then stop and wait for the answer. In a
  non-interactive run (`codex exec`) a checkpoint the prompt did not answer ends the run there; say
  which checkpoint, what is finished and what is still pending, and the command that resumes
  (`$build-app <slug>` reads §11). When a later prompt from the user answers that checkpoint, record
  the answer in §11 and continue from there without asking it again.
  `/onboard`'s setup questions follow the same rule: explain each setting, ask it separately in
  chat, and wait; a detected value is a recommendation, never an answer
  ([onboard SKILL.md](../onboard/SKILL.md) Stage 2).
- **Subagents and briefs.** Where a step says "Task", "subagents" or "dispatch" with a brief
  (`/review-app`'s reviewers, `/build-app`'s `briefs/`), use your agent's own subagents if it has
  them. Otherwise run each brief yourself, one after another, each against only its own inputs and
  writing only the files it owns; the merged result has the same shape either way.
- **Cross-agent review.** Never shell out to the agent you are running in: inside Codex, `codex` is
  the host, not an external reviewer ([cross-agent-review.md](cross-agent-review.md)).
- **No plugin hooks.** The Claude Code plugin's hooks do not run with these copies, so keep their
  three guards by hand: never run `snow streamlit deploy` or `drop`, or destructive SQL, yourself
  (deploys go through CI after the ship skill opens a PR); before ending a turn that changed an app,
  run `streamsnow review-gate classify <slug> --format json` and offer a review when it says one is
  owed; and nothing announces the skills at session start, so point the user at `build-app`.
