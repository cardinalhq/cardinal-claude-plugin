#!/usr/bin/env python3
"""Create and check the values file the user fills in for onboard-alloy.

Usage:
  onboard_env.py --init  .env.onboard-alloy   # write the template (never overwrites), chmod 600
  onboard_env.py --check .env.onboard-alloy   # validate; exit 0 when every value is usable

alloy_inventory.py and render.py take the same file with --env-file.

None of these values is a secret: Alloy reaches S3 through its IAM role, so no keys
go in this file. It is still created chmod 600, like the other Cardinal skill files.

Exit codes: 0 ok, 2 bad usage/file, 3 values missing or invalid (ask the user to fix
the file, then run --check again).
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alloy_config as ac  # noqa: E402

TEMPLATE = """\
# /cardinal:onboard-alloy — fill in the values below, save, then tell Claude "done".
# Nothing here is a password: Alloy writes to S3 through its IAM role, so no keys go here.

# Path to the Alloy config to patch, taken from its source of truth (Helm values,
# GitOps repo) — not the live ConfigMap. Relative paths are relative to this file.
ALLOY_CONFIG=

# Cardinal organization UUID (from your Cardinal Data Lake install).
CARDINAL_ORG_ID=

# Name for this cluster: lowercase letters, digits and '-', max 63 characters.
# It becomes part of the S3 path and the k8s.cluster.name label, so it can't change
# later. Use the name Grafana already uses for this cluster (often its `cluster` label).
CLUSTER_NAME=

# The Cardinal Data Lake bucket and its region.
S3_BUCKET=
AWS_REGION=

# Only for S3-compatible stores (MinIO, R2, ...): the endpoint URL. Leave empty for AWS S3.
S3_ENDPOINT=

# Only if the bucket uses SSE-KMS encryption: the KMS key ARN. Otherwise leave empty.
KMS_KEY_ARN=

# How the values above reach Alloy:
#   env      — from environment variables on the Alloy pods (one config fits many clusters)
#   literal  — written into the config itself
VALUES_MODE=env
"""

KEYS = ("ALLOY_CONFIG", "CARDINAL_ORG_ID", "CLUSTER_NAME", "S3_BUCKET", "AWS_REGION",
        "S3_ENDPOINT", "KMS_KEY_ARN", "VALUES_MODE")
REQUIRED = ("ALLOY_CONFIG", "CARDINAL_ORG_ID", "CLUSTER_NAME", "S3_BUCKET", "AWS_REGION")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLUSTER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
AWS_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d$")
KMS_RE = re.compile(r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(key|alias)/.+$")


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


def config_path(env_path: str, values: Dict[str, str]) -> str:
    p = os.path.expanduser(values.get("ALLOY_CONFIG", ""))
    return p if os.path.isabs(p) else os.path.join(os.path.dirname(os.path.abspath(env_path)), p)


def check(env_path: str, values: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """(problems, notes). Problems block the next step; notes are informational."""
    problems: List[str] = []
    notes: List[str] = []
    for k in REQUIRED:
        if not values.get(k):
            problems.append(f"{k} is empty")
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
    mode = values.get("VALUES_MODE", "env") or "env"
    if mode not in ("env", "literal"):
        problems.append('VALUES_MODE must be "env" or "literal"')
    return problems, notes


def apply_to_plan(plan: Dict, values: Dict[str, str]) -> Dict:
    """Overlay the file's values on a plan (the file wins for these fields)."""
    p = dict(plan)
    p["org_id"] = values.get("CARDINAL_ORG_ID", p.get("org_id", ""))
    p["cluster"] = values.get("CLUSTER_NAME", p.get("cluster", ""))
    p["bucket"] = values.get("S3_BUCKET", p.get("bucket", ""))
    p["region"] = values.get("AWS_REGION", p.get("region", ""))
    p["endpoint"] = values.get("S3_ENDPOINT") or None
    p["kms_key_arn"] = values.get("KMS_KEY_ARN") or None
    p["values"] = values.get("VALUES_MODE") or "env"
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
    args = ap.parse_args(argv)

    if args.init:
        if os.path.exists(args.init):
            print(f"{args.init} already exists — not overwritten")
        else:
            fd = os.open(args.init, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(TEMPLATE)
            print(f"created {args.init} — open it in an editor, fill it in, save")
        os.chmod(args.init, stat.S_IRUSR | stat.S_IWUSR)
        return 0

    try:
        values = read(args.check)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    problems, notes = check(args.check, values)
    for k in KEYS:
        v = values.get(k, "")
        bad = any(pr.startswith(k) for pr in problems)
        print(f"  {'✗' if bad else '✓'} {k:<16} {v or '(empty)'}")
    for n in notes:
        print(f"  · {n}")
    for pr in problems:
        print(f"FIX   {pr}")
    print(f"RESULT: {'INCOMPLETE' if problems else 'OK'}")
    return 3 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
