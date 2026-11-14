# Harness bundle for public, built 2026-11-14

1. Clone the repository as the harness install folder named in machines/public.md, or unpack this folder there. Update it later with git pull; a release replaces the shared files and never touches your local files.
2. Dry run and read every line: `python install.py --machine public --dry-run` (use the interpreter the machine file names; where the permission classifier blocks config edits, run the real install with the ! prefix).
3. Real run: `python install.py --machine public`. The hook tests run last and all must pass.
4. Design renders need Playwright: run python -m pip install playwright, then python -m playwright install chromium, with the interpreter the machine file names; install.py prints design: Playwright ... ready once it is.
5. Restart Claude Code and run /setup; it asks who you are, your git identities and gh account, and whether you use Codex CLI, and writes the local files.
