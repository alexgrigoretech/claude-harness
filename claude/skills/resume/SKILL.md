---
name: resume
description: Start a session from the newest handoff file and continue its next step. Use when the user says "resume", "continue from the handoff", "pick up where we left off", "where were we", or pastes a handoff path as the first message.
---

# Resume

Replaces `/goal <handoff path>` and the retyped standing-goal paragraph.

1. Find the handoff: the path the user gave, else the newest `HANDOFF-*.md` in the working directory by modification time, else the newest `~/.claude/plans/<project>-*.md` for this project. Also read `~/.claude/local/machine.md` context lines the SessionStart hook printed (branch, identity, other agents, health) and act on anything wrong there first (identity mismatch, dirty tree from another session, a foreign agent running).
2. Read the handoff fully and every file its "read next" or "resume" section names. Do not re-derive facts the handoff says not to re-derive.
3. Print, in under ten lines: the standing goal, the state (done, in progress, blocked), the next step, and the exact commands from the handoff. Then start on the next step without asking, unless the handoff says a decision or an input from the user is required first, in which case ask that one question.
4. If the handoff is older than the newest commit or memory entry, say which is newer and read both before acting.
5. Goals given to `/goal` are checkable conditions (test count, exit code, file exists), never a handoff path; this skill is the path reader.
