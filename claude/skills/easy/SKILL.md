---
name: easy
description: Explain a topic, change or decision simply using the person's own context without starting work. Use for "easy", "/easy", "explain it simply", "in plain words", "eli5", "I do not get it", "help me understand this", or "what does this mean for me".
model: sonnet
---

# Easy

Explain the argument as a topic, feature, change or decision. With no argument, explain the last thing discussed in the conversation, meaning the most recent change, finding or decision.

Answer in five short lines or fewer, using plain words and one idea per line. Say what it is in one sentence, why it matters here, one concrete example or comparison from the person's repository, conversation or data work, what changes for the person in practice, and the one catch or limit to know. Never use a generic textbook example. Skip any line that has nothing real to say.

Simplify the unfamiliar mechanism, not the person's domain. Calibrate to the Who I am section of `~/.claude/local/machine.local.md`: never explain the domains it lists, and give any new tool, flag, protocol or version quirk a one-clause gloss the first time it appears. When that section still carries the stub text, ask for the level in one line before explaining.

Use no jargon without that gloss and leave no acronym unexpanded. Use no code unless one line is the clearest way to show the thing. Do not list options, restate the question or add a closing summary.

When asked in Romanian, answer in Romanian without diacritics.

Never start or change work. If explaining the topic correctly needs a file or diff, read it, then explain.
