---
name: paste
description: Save the image on the clipboard to a file and read it into the conversation. Use when the user says "paste", "/paste", "look at my clipboard", "here is a screenshot", "read the image I copied", or when an Alt+V image paste did nothing.
---

# Paste

Alt+V image paste fails silently on some Claude Code builds (upstream issue 59661). This skill runs the clipboard helper, takes the path it prints, and reads the file so the image is in context. Typing `/paste` never touches the clipboard, so the person copies the image first and types the command second, with no ordering trap.

1. Windows only today. Take the harness install folder from `~/.claude/local/machine.md`. On macOS or Linux say in one line that the helper is Windows only and stop.
2. Run through the Bash tool, with forward slashes and the path quoted: `powershell -NoProfile -ExecutionPolicy Bypass -File "<install folder>/tools/clip_image.ps1" -NoClipboard`. The `-NoClipboard` switch leaves the image on the clipboard, since the path arrives in the tool result and nothing needs to land on the clipboard. The helper prints the saved path on success (a PNG under `%LOCALAPPDATA%\Temp\claude\paste\`, or the file itself when an image file was copied in Explorer) and `clip_image: no image on the clipboard` with exit 1 otherwise; report that message in one line and stop. Where a permission prompt denies the command, tell the person to run the `!` form from `machine.md` instead.
3. Read the file with the Read tool so the image is visible.
4. Reply with the path on one line, then do whatever the person asked about the image. With no request, describe what it shows in two lines.

The paste folder is cleaned of files older than seven days each time the helper runs. When an image matters for a handoff, copy it into the project's `.scratch/` and reference that path; never move it into a project folder unasked.
