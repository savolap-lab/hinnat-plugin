"""Supplier logins. Values come from the environment — never from code,
never from the command line.

    from lib import credentials
    user, password = credentials.get("sonepar")

Resolution order, first hit wins:

  1. a real environment variable   HINNAT_SONEPAR_USER / _PASS
  2. the .env file at the repo root (gitignored)

A real environment variable always beats .env, so a scheduled job can inject
credentials without anyone editing a file.

Nothing here logs, prints, or returns a password as part of an error message.
`redact()` exists for the cases where a value has to appear in output.

Standard library only, like the rest of the query side.
"""
import os
import stat

from . import paths

ROOT = paths.ROOT
ENV_FILE = paths.ENV_FILE
SECRETS_DIR = paths.SECRETS_DIR

PREFIX = "HINNAT"


class CredentialsMissing(Exception):
    """Raised when a supplier's login is not configured anywhere."""


def var_names(supplier):
    """('sonepar') -> ('HINNAT_SONEPAR_USER', 'HINNAT_SONEPAR_PASS')"""
    s = supplier.strip().upper().replace("-", "_").replace(" ", "_")
    if not s:
        raise ValueError("supplier must not be empty")
    return f"{PREFIX}_{s}_USER", f"{PREFIX}_{s}_PASS"


def parse_env(text):
    """Parse .env content into a dict. Deliberately small and predictable.

    Accepts `KEY=value`, `export KEY=value`, blank lines and `#` comments.
    Strips one matching pair of surrounding quotes; keeps inner spaces so a
    password containing '#' or '=' survives intact.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue                       # not a KEY=VALUE line; ignore it
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def check_permissions(path=ENV_FILE):
    """Return a warning string if the file is readable by anyone else."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (f"{path} is readable by other users on this machine. "
                f"Fix with:  chmod 600 {path}")
    return None


def load_env(path=ENV_FILE, environ=None):
    """Merge .env into `environ` without overwriting what is already set.

    Returns the list of keys it actually set, so callers can report what came
    from the file without ever touching the values.
    """
    environ = os.environ if environ is None else environ
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = parse_env(fh.read())
    except FileNotFoundError:
        return []
    added = []
    for k, v in parsed.items():
        if k not in environ:               # a real env var always wins
            environ[k] = v
            added.append(k)
    return added


def get(supplier, environ=None, env_file=ENV_FILE):
    """Return (username, password) for a supplier, or raise CredentialsMissing."""
    environ = os.environ if environ is None else environ
    load_env(env_file, environ)
    user_key, pass_key = var_names(supplier)
    user, password = environ.get(user_key), environ.get(pass_key)
    if not user or not password:
        missing = [k for k, v in ((user_key, user), (pass_key, password)) if not v]
        raise CredentialsMissing(
            f"{supplier}: {' and '.join(missing)} not set.\n"
            f"Add them to {env_file} (copy .env.example if it does not exist "
            f"yet), or export them in your shell. Never pass a password as a "
            f"command-line argument — it lands in your shell history.")
    return user, password


def configured(environ=None, env_file=ENV_FILE):
    """Suppliers that have both a username and a password available."""
    environ = os.environ if environ is None else environ
    load_env(env_file, environ)
    names = set()
    for key in environ:
        if key.startswith(f"{PREFIX}_") and key.endswith("_USER"):
            names.add(key[len(PREFIX) + 1:-len("_USER")])
    out = []
    for n in sorted(names):
        u, p = var_names(n)
        if environ.get(u) and environ.get(p):
            out.append(n.lower())
    return out


def state_path(supplier):
    """Where Playwright's logged-in session for this supplier is kept.

    This file is as sensitive as the password — it is a live session. It is
    gitignored, machine-local, and must never be shared or committed.
    """
    s = supplier.strip().lower().replace(" ", "_")
    return os.path.join(SECRETS_DIR, f"{s}_state.json")


def ensure_secrets_dir():
    """Create .secrets/ with owner-only permissions."""
    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(SECRETS_DIR, 0o700)
    except OSError:
        pass
    return SECRETS_DIR


def redact(value, keep=2):
    """'hunter2' -> 'hu***'. For output that must mention a value at all."""
    if not value:
        return ""
    return value[:keep] + "***"
