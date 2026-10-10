import shutil
import subprocess
import sys


def main():
    if len(sys.argv) != 2 or sys.argv[1] != "get":
        return 0
    request = {}
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            request[key] = value
    host = request.get("host", "").lower()
    username = request.get("username")
    if host not in ("github.com", "gist.github.com") or not username or request.get("protocol", "https") != "https":
        return 0
    token_host = "github.com" if host == "gist.github.com" else host
    gh = shutil.which("gh")
    if gh is None:
        return 0
    try:
        completed = subprocess.run(
            [gh, "auth", "token", "--user", username, "--hostname", token_host],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    token = completed.stdout.strip()
    if completed.returncode or not token or "\n" in token or "\r" in token:
        return 0
    sys.stdout.write(f"username={username}\npassword={token}\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
