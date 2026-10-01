#!/usr/bin/env python3
"""Create and check the values file the user fills in for onboard-alloy.

Usage:
  onboard_env.py --init  .env.onboard-alloy   # write the template (never overwrites), chmod 600
  onboard_env.py --init  .env.onboard-alloy --from-connection --set ALLOY_CONFIG=... --set CLUSTER_NAME=...
                                              # prefill what's already known, for the user to confirm
  onboard_env.py --check .env.onboard-alloy [--from-connection]
                                              # validate; exit 0 when every value is usable

--from-connection reads the non-secret state the agent's Cardinal connect saved
(cardinal.json: host, org, ingest endpoint; never the secrets file) from
CARDINAL_AGENT_HOME, else the first of ~/.claude, ~/.codex, ~/.cursor, ~/.gemini that has
one, or from the path given. It prefills the org, and the ingest endpoint once
--set TARGET=saas says the data goes to Cardinal SaaS. It never decides TARGET: a
customer whose data lake runs in their own VPC connects to the same app.cardinalhq.io
as a SaaS customer, so ask the user and pass the answer with --set TARGET=saas|s3.

alloy_inventory.py and render.py take the same file with --env-file.

None of these values is a secret: with TARGET=s3 Alloy reaches S3 through its IAM
role; with TARGET=saas the API key reaches Alloy as an env var from a Kubernetes
Secret (or the host's env file), and this file holds only that variable's name. It is
still created chmod 600, like the other Cardinal skill files.

Exit codes: 0 ok, 2 bad usage/file, 3 values missing or invalid (ask the user to fix
the file, then run --check again), 4 --init found an existing file that differs from
what it would prefill (DIFFERS lines; --replace starts a fresh one, keeping the old).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alloy_config as ac  # noqa: E402

TEMPLATE = """\
# /cardinal:onboard-alloy — fill in the values below, save, then tell Claude "done".
# Nothing here is a password: never put an AWS key or a Cardinal API key in this file.

# Where Cardinal receives the data (required, no default):
#   saas  — Cardinal SaaS: Cardinal stores the data; Alloy sends OTLP/HTTP with an API key
#   s3    — your own Cardinal Data Lake in your VPC (Lakerunner on your bucket); Alloy
#           writes files to the bucket. Your Cardinal login may still be app.cardinalhq.io.
TARGET=

# Path to the Alloy config to patch, taken from its source of truth (Helm values,
# GitOps repo) — not the live ConfigMap. Relative paths are relative to this file.
ALLOY_CONFIG=

# Cardinal organization UUID (from your Cardinal Data Lake install).
# Required for s3; optional for saas (the API key identifies the organization).
CARDINAL_ORG_ID=

# Name for this cluster: lowercase letters, digits and '-', max 63 characters.
# It becomes the k8s.cluster.name label (and part of the S3 path), so it can't change
# later. Use the name Grafana already uses for this cluster (often its `cluster` label).
CLUSTER_NAME=

# Where this Alloy runs:
#   kubernetes — pods (Helm chart, ConfigMap, GitOps); env vars and the API key come from the pod spec
#   host       — a service on a machine (Homebrew, a Linux package under systemd); env vars come
#                from the service's env file, and no Kubernetes metadata is added
RUNTIME=kubernetes

# --- TARGET=s3 only -------------------------------------------------------------
# The Cardinal Data Lake bucket and its region.
S3_BUCKET=
AWS_REGION=

# Only for S3-compatible stores (MinIO, R2, ...): the endpoint URL. Leave empty for AWS S3.
S3_ENDPOINT=

# Only if the bucket uses SSE-KMS encryption: the KMS key ARN. Otherwise leave empty.
KMS_KEY_ARN=

# --- TARGET=saas only -----------------------------------------------------------
# Cardinal's OTLP/HTTP intake for your region, without /v1/logs etc., e.g.
# https://otelhttp.intake.us-east-2.aws.cardinalhq.io
CARDINAL_INGEST_ENDPOINT=

# Name of the env var Alloy will read the Cardinal API key from (the key itself goes in
# a Kubernetes Secret, or the service's env file for RUNTIME=host, never here).
# Default: CARDINAL_API_KEY
CARDINAL_API_KEY_ENV=CARDINAL_API_KEY

# How the values above reach Alloy:
#   env      — from environment variables on the Alloy pods (one config fits many clusters)
#   literal  — written into the config itself
VALUES_MODE=env
"""

KEYS = ("TARGET", "ALLOY_CONFIG", "CARDINAL_ORG_ID", "CLUSTER_NAME", "RUNTIME", "S3_BUCKET", "AWS_REGION",
        "S3_ENDPOINT", "KMS_KEY_ARN", "CARDINAL_INGEST_ENDPOINT", "CARDINAL_API_KEY_ENV", "VALUES_MODE")
TARGETS = ("s3", "saas")
RUNTIMES = ("kubernetes", "host")
# Config locations of host installs: Homebrew (Apple silicon, Intel) and the Linux packages.
HOST_CONFIG_DIRS = ("/opt/homebrew/etc/alloy", "/usr/local/etc/alloy", "/etc/alloy")
AGENT_HOMES = ("~/.claude", "~/.codex", "~/.cursor", "~/.gemini")
EXIT_DIFFERS = 4
REQUIRED = {
    "s3": ("ALLOY_CONFIG", "CARDINAL_ORG_ID", "CLUSTER_NAME", "S3_BUCKET", "AWS_REGION"),
    "saas": ("ALLOY_CONFIG", "CLUSTER_NAME", "CARDINAL_INGEST_ENDPOINT"),
}
DEFAULT_API_KEY_ENV = "CARDINAL_API_KEY"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLUSTER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
AWS_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d$")
KMS_RE = re.compile(r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(key|alias)/.+$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def read(path: str) -> Dict[str, str]:
    """KEY=VALUE lines; comments, blanks and <placeholder> values count as empty."""
    out: Dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if v.startswith("<") and v.endswith(">"):
                v = ""
            out[k.strip()] = v
    return out


def keys_in_file(path: str) -> List[str]:
    """Keys the file sets at all (even empty). A file from an older template lacks some."""
    with open(path, encoding="utf-8") as f:
        return [line.split("=", 1)[0].strip() for line in f
                if "=" in line and not line.strip().startswith("#")]


def connection_file(path: Optional[str] = None) -> Optional[str]:
    """The cardinal.json the agent's connect wrote, or None when not connected."""
    if path:
        return os.path.expanduser(path)
    homes = [os.environ["CARDINAL_AGENT_HOME"]] if os.environ.get("CARDINAL_AGENT_HOME") else AGENT_HOMES
    for home in map(os.path.expanduser, homes):
        p = os.path.join(home, "cardinal.json")
        if os.path.isfile(p):
            return p
    return None


def connection(path: Optional[str] = None) -> Dict[str, str]:
    """Non-secret facts from the connect state: {file, host, org_id, org_slug, ingest_endpoint}
    (missing ones absent), or {} when not connected. Never reads the secrets file."""
    p = connection_file(path)
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    out = {k: str(state[k]) for k in ("host", "org_id", "org_slug", "ingest_endpoint") if state.get(k)}
    if out:
        out["file"] = p
    return out


def connection_values(conn: Dict[str, str], target: Optional[str]) -> Dict[str, str]:
    """Values file entries the connection answers: the org, and for target saas the ingest
    endpoint. Never TARGET itself: SaaS and in-VPC customers share the same control plane."""
    out: Dict[str, str] = {}
    if UUID_RE.match(conn.get("org_id", "")):
        out["CARDINAL_ORG_ID"] = conn["org_id"]
    ie = conn.get("ingest_endpoint", "").rstrip("/")
    if target == "saas" and re.match(r"^https://[^/\s]+$", ie):
        out["CARDINAL_INGEST_ENDPOINT"] = ie
    return out


def guess_runtime(config: str) -> Optional[str]:
    """host when the config sits where a Homebrew or Linux-package Alloy reads it."""
    p = os.path.abspath(os.path.expanduser(config))
    return "host" if any(p == d or p.startswith(d + "/") for d in HOST_CONFIG_DIRS) else None


def parse_sets(pairs: List[str]) -> Dict[str, str]:
    """--set KEY=VALUE pairs; raises ValueError (never echoing a value) on a bad one."""
    out: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError("--set takes KEY=VALUE")
        k, v = (s.strip() for s in pair.split("=", 1))
        if k not in KEYS:
            raise ValueError(f"--set: unknown key {k} (one of {', '.join(KEYS)})")
        if k == "CARDINAL_API_KEY_ENV" and not ENV_NAME_RE.match(v):
            raise ValueError("--set CARDINAL_API_KEY_ENV takes an env var NAME like CARDINAL_API_KEY, "
                             "never the key itself")
        out[k] = v
    return out


def fill(template: str, values: Dict[str, str]) -> str:
    """The template with KEY= lines set to these values."""
    for k, v in values.items():
        template = re.sub(rf"(?m)^{k}=.*$", lambda _m, k=k, v=v: f"{k}={v}", template)
    return template


def differences(path: str, wanted: Dict[str, str]) -> List[str]:
    """How an existing file differs from what --init would prefill."""
    have = read(path)
    present = set(keys_in_file(path))
    out = []
    missing = [k for k in KEYS if k not in present]
    if missing:
        out.append(f"file is from an older template: no {', '.join(missing)} line"
                   + (" (TARGET then defaults to s3)" if "TARGET" in missing else ""))
    for k, v in wanted.items():
        if k in present and have.get(k, "") != v:
            out.append(f"{k}: file has {have.get(k) or '(empty)'}, expected {v}")
    return out


def config_path(env_path: str, values: Dict[str, str]) -> str:
    p = os.path.expanduser(values.get("ALLOY_CONFIG", ""))
    return p if os.path.isabs(p) else os.path.join(os.path.dirname(os.path.abspath(env_path)), p)


def check(env_path: str, values: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """(problems, notes). Problems block the next step; notes are informational."""
    problems: List[str] = []
    notes: List[str] = []
    # No TARGET line at all is an older template, from when s3 was the only target.
    target = values.get("TARGET", "s3")
    if not target:
        problems.append('TARGET is empty: "saas" (Cardinal SaaS stores the data) or '
                        '"s3" (your own Cardinal Data Lake in your VPC)')
    elif target not in TARGETS:
        problems.append('TARGET must be "s3" or "saas"')
    for k in REQUIRED.get(target, ()):
        if not values.get(k):
            problems.append(f"{k} is empty")
    if target == "saas":
        unused = [k for k in ("S3_BUCKET", "AWS_REGION", "S3_ENDPOINT", "KMS_KEY_ARN") if values.get(k)]
        if unused:
            notes.append(f"TARGET=saas: {', '.join(unused)} not used")
    elif values.get("CARDINAL_INGEST_ENDPOINT"):
        notes.append("TARGET=s3: CARDINAL_INGEST_ENDPOINT not used")
    unknown = sorted(set(values) - set(KEYS))
    if unknown:
        notes.append(f"ignored unknown keys: {', '.join(unknown)}")

    if values.get("ALLOY_CONFIG"):
        cfg = config_path(env_path, values)
        if not os.path.isfile(cfg):
            problems.append(f"ALLOY_CONFIG: no file at {cfg}")
        else:
            try:
                with open(cfg, encoding="utf-8") as f:
                    g = ac.graph(ac.parse(f.read()))
                notes.append(f"ALLOY_CONFIG parses: {len(g.nodes)} components, {len(g.sinks())} exporters")
            except ac.AlloySyntaxError as e:
                problems.append(f"ALLOY_CONFIG doesn't parse as Alloy config: {e}")
    org = values.get("CARDINAL_ORG_ID", "")
    if org and not UUID_RE.match(org):
        problems.append("CARDINAL_ORG_ID must be a lowercase UUID like 0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b")
    cl = values.get("CLUSTER_NAME", "")
    if cl and not CLUSTER_RE.match(cl):
        problems.append("CLUSTER_NAME: use lowercase letters, digits and '-' (max 63), e.g. prod-us-east-1")
    b = values.get("S3_BUCKET", "")
    if b and (b.startswith("s3://") or "/" in b):
        problems.append("S3_BUCKET: the bucket name only, without s3:// or a path")
    elif b and not BUCKET_RE.match(b):
        problems.append("S3_BUCKET isn't a valid bucket name (lowercase letters, digits, '.', '-')")
    ep = values.get("S3_ENDPOINT", "")
    if ep and not re.match(r"^https?://[^/\s]+", ep):
        problems.append("S3_ENDPOINT must be a URL like https://minio.example.com")
    r = values.get("AWS_REGION", "")
    if r and not ep and not AWS_REGION_RE.match(r):
        problems.append(f"AWS_REGION {r!r} doesn't look like an AWS region (e.g. us-east-1)")
    k = values.get("KMS_KEY_ARN", "")
    if k and not KMS_RE.match(k):
        problems.append("KMS_KEY_ARN must look like arn:aws:kms:<region>:<account>:key/<id>")
    ie = values.get("CARDINAL_INGEST_ENDPOINT", "")
    if ie and not re.match(r"^https?://[^/\s]+/?$", ie):
        problems.append("CARDINAL_INGEST_ENDPOINT must be a base URL like "
                        "https://otelhttp.intake.us-east-2.aws.cardinalhq.io (no /v1/... path)")
    elif ie.startswith("http://") and target == "saas":
        notes.append("CARDINAL_INGEST_ENDPOINT is http://: the API key would be sent unencrypted")
    kn = values.get("CARDINAL_API_KEY_ENV", "")
    if kn and not ENV_NAME_RE.match(kn):
        # Most likely the key itself was pasted here. Don't echo it back.
        problems.append("CARDINAL_API_KEY_ENV must be an env var NAME like CARDINAL_API_KEY "
                        "(uppercase); the key itself goes in a Kubernetes Secret. If you pasted "
                        "the key here, remove it and rotate it")
    mode = values.get("VALUES_MODE", "env") or "env"
    if mode not in ("env", "literal"):
        problems.append('VALUES_MODE must be "env" or "literal"')
    rt = values.get("RUNTIME", "kubernetes") or "kubernetes"
    if rt not in RUNTIMES:
        problems.append('RUNTIME must be "kubernetes" or "host"')
    elif rt == "kubernetes" and values.get("ALLOY_CONFIG") and guess_runtime(config_path(env_path, values)):
        notes.append("ALLOY_CONFIG is where a Homebrew or Linux-package Alloy reads its config: "
                     "RUNTIME=host may fit better")
    try:
        missing = [k for k in KEYS if k not in keys_in_file(env_path)]
    except OSError:
        missing = []
    if missing:
        notes.append(f"file is from an older template, no {', '.join(missing)} line: "
                     "re-create it with --init --replace")
    return problems, notes


def connection_notes(values: Dict[str, str], conn: Dict[str, str]) -> List[str]:
    """Where the file points somewhere other than the Cardinal this agent is connected to."""
    if not conn:
        return ["not connected to Cardinal: nothing to compare the org and endpoint with"]
    who = f"{conn.get('org_slug') or conn.get('org_id', '?')} on {conn.get('host', '?')}"
    out = []
    for k, v in connection_values(conn, values.get("TARGET")).items():
        if (values.get(k) or "").rstrip("/") != v:
            out.append(f"{k} is {values.get(k) or '(empty)'} but the Cardinal connection ({who}) says {v}")
    return out


def apply_to_plan(plan: Dict, values: Dict[str, str]) -> Dict:
    """Overlay the file's values on a plan (the file wins for these fields)."""
    p = dict(plan)
    p["target"] = values.get("TARGET") or "s3"
    p["ingest_endpoint"] = (values.get("CARDINAL_INGEST_ENDPOINT") or "").rstrip("/") or None
    p["api_key_env"] = values.get("CARDINAL_API_KEY_ENV") or DEFAULT_API_KEY_ENV
    p["org_id"] = values.get("CARDINAL_ORG_ID", p.get("org_id", ""))
    p["cluster"] = values.get("CLUSTER_NAME", p.get("cluster", ""))
    p["bucket"] = values.get("S3_BUCKET", p.get("bucket", ""))
    p["region"] = values.get("AWS_REGION", p.get("region", ""))
    p["endpoint"] = values.get("S3_ENDPOINT") or None
    p["kms_key_arn"] = values.get("KMS_KEY_ARN") or None
    p["values"] = values.get("VALUES_MODE") or "env"
    p["runtime"] = values.get("RUNTIME") or "kubernetes"
    if p["runtime"] == "host":
        p["k8sattributes"] = False   # no Kubernetes API to ask
    return p


def load_checked(env_path: str) -> Tuple[Dict[str, str], str]:
    """For the other scripts: (values, config path). Exits 3 if the file isn't usable."""
    values = read(env_path)
    problems, _ = check(env_path, values)
    if problems:
        print(f"{env_path} isn't complete:", file=sys.stderr)
        for pr in problems:
            print(f"  ✗ {pr}", file=sys.stderr)
        sys.exit(3)
    return values, config_path(env_path, values)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", metavar="FILE")
    g.add_argument("--check", metavar="FILE")
    ap.add_argument("--from-connection", nargs="?", const="", metavar="CARDINAL_JSON",
                    help="prefill (--init) or compare (--check) the org, and for TARGET=saas the "
                         "ingest endpoint, with the agent's Cardinal connection")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="--init: prefill a value already known (repeatable; never the API key)")
    ap.add_argument("--replace", action="store_true",
                    help="--init: move an existing file to FILE.bak-<time> and write a fresh one")
    args = ap.parse_args(argv)
    use_conn = args.from_connection is not None
    conn = connection(args.from_connection or None) if use_conn else {}

    if args.init:
        try:
            sets = parse_sets(args.set)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if sets.get("TARGET", "saas") not in TARGETS:
            print('error: --set TARGET takes "saas" or "s3"', file=sys.stderr)
            return 2
        prefill: Dict[str, Tuple[str, str]] = {}   # key -> (value, where it came from)
        for k, v in connection_values(conn, sets.get("TARGET")).items():
            prefill[k] = (v, f"Cardinal connection ({conn.get('org_slug') or conn.get('host')})")
        for k, v in sets.items():
            prefill[k] = (v, "--set")
        if "ALLOY_CONFIG" in prefill and "RUNTIME" not in prefill:
            rt = guess_runtime(prefill["ALLOY_CONFIG"][0])
            if rt:
                prefill["RUNTIME"] = (rt, "the config's location (Homebrew / Linux package)")
        if use_conn and not conn:
            print("not connected to Cardinal: org and endpoint left for the user")
        if "TARGET" not in prefill:
            print("TARGET left empty: ask the user whether the data goes to Cardinal SaaS or to "
                  "their own Cardinal Data Lake in their VPC (the connection can't tell them apart), "
                  "then pass --set TARGET=saas|s3")
        wanted = {k: v for k, (v, _) in prefill.items()}
        if os.path.exists(args.init) and not args.replace:
            print(f"{args.init} already exists — not overwritten")
            diffs = differences(args.init, wanted)
            for d in diffs:
                print(f"DIFFERS  {d}")
            os.chmod(args.init, stat.S_IRUSR | stat.S_IWUSR)
            if diffs:
                print("Ask the user whether to keep it or start fresh with --replace "
                      "(the old file is kept as a .bak).")
                return EXIT_DIFFERS
            return 0
        if os.path.exists(args.init):
            bak = f"{args.init}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            os.replace(args.init, bak)
            print(f"moved the old file to {bak}")
        fd = os.open(args.init, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(fill(TEMPLATE, wanted))
        os.chmod(args.init, stat.S_IRUSR | stat.S_IWUSR)
        print(f"created {args.init} — open it in an editor, "
              + ("check the prefilled values, fill in the rest, save" if prefill else "fill it in, save"))
        for k, (v, src) in prefill.items():
            print(f"  prefilled {k}={v}  (from {src})")
        return 0

    try:
        values = read(args.check)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    problems, notes = check(args.check, values)
    if use_conn:
        notes += connection_notes(values, conn)
    for k in KEYS:
        v = values.get(k, "")
        bad = any(re.match(rf"{k}\b", pr) for pr in problems)
        if bad and k == "CARDINAL_API_KEY_ENV":
            v = "(hidden: may be a key)"
        print(f"  {'✗' if bad else '✓'} {k:<24} {v or '(empty)'}")
    for n in notes:
        print(f"  · {n}")
    for pr in problems:
        print(f"FIX   {pr}")
    print(f"RESULT: {'INCOMPLETE' if problems else 'OK'}")
    return 3 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
