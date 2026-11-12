"""Runtime settings: nothing sensitive is stored in GitHub.

Secrets and connection settings are read when kcbudget starts, from the machine that runs it
(a self-hosted runner, a laptop, a pod), in this order (first non-empty value wins):

  1. The process environment, e.g. the runner service's own env or a Kubernetes Secret mounted as env.
  2. KCBUDGET_SECRETS_COMMAND: the customer carve-out. A command you supply that prints the settings
     at runtime, as a JSON object or KEY=VALUE lines (Vault, AWS Secrets Manager, Azure Key Vault,
     1Password, ...). See examples/secrets-providers/. It runs without a shell, with a 60s timeout.
  3. KCBUDGET_ENV_FILE: a KEY=VALUE file, default ~/.kcbudget/<KCBUDGET_ENV>.env (chmod 600).

Only these keys are accepted, so a provider can't change PATH or other process settings:
  KUBECOST_*  KCBUDGET_*  KCRESIZE_SMTP_*
Webhook URLs: KCBUDGET_WEBHOOKS (JSON) or KCBUDGET_WEBHOOKS_FILE (path to the same JSON).
In GitHub Actions every loaded value is masked in the log.
"""
import json
import os
import shlex
import stat
import subprocess

ALLOWED_PREFIXES = ("KUBECOST_", "KCBUDGET_", "KCRESIZE_SMTP_")
NOT_FROM_PROVIDERS = {"KCBUDGET_SECRETS_COMMAND", "KCBUDGET_ENV_FILE"}
SECRETISH = ("PASSWORD", "TOKEN", "AUTH", "WEBHOOK", "SECRET", "USER")


class RuntimeError_(RuntimeError):
    pass


def parse(text):
    """JSON object or KEY=VALUE lines (# comments, optional 'export ', optional quotes)."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): v if isinstance(v, str) else json.dumps(v) for k, v in data.items()}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def _allowed(k):
    return k.startswith(ALLOWED_PREFIXES) and k not in NOT_FROM_PROVIDERS


def default_env_file():
    env = os.environ.get("KCBUDGET_ENV", "") or "default"
    return os.path.join(os.path.expanduser("~"), ".kcbudget", f"{env}.env")


def _from_file(path, report):
    if not path or not os.path.isfile(path):
        return {}
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        report.append(f"warning: {path} is readable by other users; run chmod 600 {path}")
    with open(path) as f:
        return parse(f.read())


def _from_command(cmd, report):
    if not cmd:
        return {}
    try:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError_(f"KCBUDGET_SECRETS_COMMAND failed to run: {e}")
    if r.returncode != 0:
        raise RuntimeError_(f"KCBUDGET_SECRETS_COMMAND exited {r.returncode}: {(r.stderr or '').strip()[:300]}")
    try:
        return parse(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError_(f"KCBUDGET_SECRETS_COMMAND printed invalid JSON: {e}")


def load(environ=None):
    """Fill missing settings in environ (default os.environ). Returns a list of report lines (no values)."""
    environ = os.environ if environ is None else environ
    report, sources = [], []
    cmd = environ.get("KCBUDGET_SECRETS_COMMAND", "").strip()
    if cmd:
        sources.append(("secrets command", _from_command(cmd, report)))
    path = environ.get("KCBUDGET_ENV_FILE", "").strip() or default_env_file()
    sources.append((path, _from_file(path, report)))
    for label, values in sources:
        rejected = sorted(k for k in values if not _allowed(k))
        if rejected:
            report.append(f"warning: {label}: ignored keys outside {', '.join(ALLOWED_PREFIXES)}: {', '.join(rejected)}")
        used = []
        for k, v in values.items():
            if _allowed(k) and v != "" and not environ.get(k):
                environ[k] = v
                used.append(k)
                if environ.get("GITHUB_ACTIONS") == "true" and any(s in k for s in SECRETISH):
                    for line in str(v).splitlines():
                        if line.strip():
                            print(f"::add-mask::{line.strip()}")
        if used:
            report.append(f"loaded {len(used)} setting(s) from {label}: {', '.join(sorted(used))}")
    wf = environ.get("KCBUDGET_WEBHOOKS_FILE", "").strip()
    if wf and not environ.get("KCBUDGET_WEBHOOKS"):
        if not os.path.isfile(wf):
            report.append(f"warning: KCBUDGET_WEBHOOKS_FILE {wf} not found")
        else:
            with open(wf) as f:
                raw = f.read()
            environ["KCBUDGET_WEBHOOKS"] = raw
            report.append(f"loaded webhooks from {wf}")
            if environ.get("GITHUB_ACTIONS") == "true":
                try:
                    for kind in json.loads(raw).values():
                        for url in (kind or {}).values() if isinstance(kind, dict) else []:
                            print(f"::add-mask::{url}")
                except (json.JSONDecodeError, AttributeError):
                    pass
    return report


def status(environ=None):
    """Which settings are present (names only) - safe to print."""
    environ = os.environ if environ is None else environ
    keys = ["KUBECOST_URL", "KUBECOST_AUTH_HEADER", "KUBECOST_BASIC_USER", "KCBUDGET_WEBHOOKS", "KCRESIZE_SMTP_HOST"]
    return {k: bool(environ.get(k)) for k in keys}
