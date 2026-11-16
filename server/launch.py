"""Launcher for the WING MCP server.

Claude Desktop starts a Python extension with whatever python3 it finds
first, which may be too old or may lack the mcp package even when another
interpreter on the machine has it. This runs under that interpreter, checks
it, and if it can't host the server, re-executes server.py under the first
interpreter that can. Kept to syntax any Python 3 parses, so even an old
system python3 gets far enough to hand off.

To force one interpreter: set WING_MCP_PYTHON to its full path
(on macOS: launchctl setenv WING_MCP_PYTHON /opt/homebrew/bin/python3.12,
then restart Claude Desktop).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
CHECK = "import sys; assert sys.version_info >= (3, 10); import mcp.server.fastmcp"


def usable_here():
    if sys.version_info < (3, 10):
        return False
    try:
        import mcp.server.fastmcp  # noqa: F401
        return True
    except Exception:
        return False


def candidates():
    env = os.environ.get("WING_MCP_PYTHON")
    if env:
        yield env
    home = os.path.expanduser("~")
    for v in ("3.14", "3.13", "3.12", "3.11", "3.10", ""):
        for d in ("/opt/homebrew/bin", "/usr/local/bin",
                  "/Library/Frameworks/Python.framework/Versions/%s/bin" % (v or "Current"),
                  os.path.join(home, ".pyenv/shims"), os.path.join(home, ".local/bin")):
            yield os.path.join(d, "python" + (v or "3"))
    if os.name == "nt":
        yield "py"


def main():
    if usable_here():
        sys.argv[0] = SERVER
        sys.path.insert(0, HERE)
        import runpy
        runpy.run_path(SERVER, run_name="__main__")
        return
    tried = []
    for py in candidates():
        if py == sys.executable or py in tried:
            continue
        tried.append(py)
        if os.sep in py and not os.path.exists(py):
            continue
        try:
            ok = subprocess.call([py, "-c", CHECK], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=20) == 0
        except Exception:
            ok = False
        if ok:
            sys.stderr.write("wing-mcp: %s cannot host the server; using %s\n"
                             % (sys.executable, py))
            sys.stderr.flush()
            if os.name == "nt":
                sys.exit(subprocess.call([py, SERVER] + sys.argv[1:]))
            os.execv(py if os.sep in py else _which(py), [py, SERVER] + sys.argv[1:])
    sys.stderr.write(
        "wing-mcp: no Python 3.10+ with the mcp package found.\n"
        "  This interpreter: %s (%s)\n"
        "  Fix: %s -m pip install 'mcp<2'   (add --break-system-packages for Homebrew)\n"
        "  or set WING_MCP_PYTHON to an interpreter that has it.\n"
        % (sys.executable, sys.version.split()[0], sys.executable))
    sys.exit(1)


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return name


if __name__ == "__main__":
    main()
