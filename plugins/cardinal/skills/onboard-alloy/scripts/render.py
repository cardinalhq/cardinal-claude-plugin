#!/usr/bin/env python3
"""Render the Cardinal branch into a customer's Alloy config from an approved plan.

Offline. Reads the customer's config and plan.json (from alloy_inventory.py, reviewed
and approved), and writes the patched config plus everything their team needs to
roll it out. Nothing is applied anywhere. The output is linted before it is written;
on a lint error nothing is written and the exit code is 1.

Usage:
  render.py --env-file .env.onboard-alloy --plan onboard/<cluster>/plan.json --out onboard/<cluster>/out
  render.py --config config.alloy --plan onboard/<cluster>/plan.json --out onboard/<cluster>/out

With --env-file, the config path comes from the file, and the file's values (org,
cluster, bucket, region, endpoint, KMS key, values mode) override plan.json's.

Writes:
  <out>/config.alloy          the customer's config + Cardinal taps + the managed Cardinal block
  <out>/cardinal.alloy        the managed Cardinal block on its own (for review)
  <out>/changes.review.diff   unified diff against the original, secrets masked (for display only)
  <out>/env.json              env vars the Alloy pods need (values: "env" mode)
  <out>/iam-policy.json       least-privilege S3 (and KMS) policy for Alloy's role
  <out>/render.json           summary: taps applied, components added, lint findings

Re-running on an already-patched config updates the Cardinal branch in place.

Exit codes: 0 ok, 1 lint errors, 2 bad input.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alloy_config as ac  # noqa: E402
import alloy_inventory as inv  # noqa: E402
import lint_config  # noqa: E402
import onboard_env  # noqa: E402

VERSION = 1
BEGIN = f"// >>> cardinal onboard-alloy v{VERSION}"
END = "// <<< cardinal onboard-alloy"
BEGIN_RE = re.compile(r"^[ \t]*// >>> cardinal onboard-alloy v\d+.*$", re.M)
END_RE = re.compile(r"^[ \t]*// <<< cardinal onboard-alloy.*$\n?", re.M)

# As of Alloy v1.20.1, otelcol.exporter.awss3 is "experimental" and
# otelcol.processor.cumulativetodelta is "public-preview". Alloy rejects the *whole*
# config (Grafana pipelines included) unless it runs with this stability level.
STABILITY_LEVEL = "experimental"
TESTED_ALLOY = "v1.20.1"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLUSTER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

K8S_METADATA = [
    "k8s.namespace.name", "k8s.pod.name", "k8s.pod.uid",
    "k8s.deployment.name", "k8s.daemonset.name", "k8s.statefulset.name",
    "k8s.node.name", "k8s.container.name",
]

C = ac.MANAGED_LABEL
K8S = f"otelcol.processor.k8sattributes.{C}"
TRANSFORM = f"otelcol.processor.transform.{C}"
DELTA = f"otelcol.processor.cumulativetodelta.{C}"
BATCH = f"otelcol.processor.batch.{C}"
S3 = f"otelcol.exporter.awss3.{C}"
PROM_BRIDGE = f"otelcol.receiver.prometheus.{C}"
LOKI_BRIDGE = f"otelcol.receiver.loki.{C}"


class PlanError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def validate_plan(plan: Dict) -> List[str]:
    """Raise PlanError on anything that would render a wrong config; return warnings."""
    errs, warns = [], []
    if not UUID_RE.match(str(plan.get("org_id", ""))):
        errs.append("org_id must be the Cardinal organization UUID (lowercase)")
    if not CLUSTER_RE.match(str(plan.get("cluster", ""))):
        errs.append("cluster must be lowercase letters, digits and '-' (max 63), e.g. prod-us-east-1")
    if plan.get("values") not in ("env", "literal"):
        errs.append('values must be "env" (read from pod env vars) or "literal" (written into the config)')
    if plan.get("values") == "literal":
        for k in ("bucket", "region"):
            if not plan.get(k):
                errs.append(f'{k} is required when values is "literal"')
    if not plan.get("bucket"):
        warns.append("bucket not set: iam-policy.json uses a <bucket> placeholder")
    batch = plan.get("batch") or {}
    for k, typ in (("send_batch_size", int), ("send_batch_max_size", int), ("timeout", str)):
        if not isinstance(batch.get(k), typ):
            errs.append(f"batch.{k} must be a {typ.__name__}")
    if batch and batch != inv.DEFAULT_BATCH:
        warns.append(f"batch differs from the Cardinal gateway defaults {inv.DEFAULT_BATCH}; "
                     "smaller batches mean more S3 PUTs")
    enabled = [t for t in plan.get("taps", []) if t.get("enabled")]
    if not enabled:
        errs.append("no enabled taps: nothing would be sent to Cardinal")
    for t in enabled:
        if t.get("kind") not in ("otlp", "prometheus", "loki"):
            errs.append(f"tap {t.get('component')}.{t.get('attr')}: kind must be otlp, prometheus or loki")
        if t.get("kind") == "otlp" and t.get("signal") not in ac.SIGNALS:
            errs.append(f"tap {t.get('component')}.{t.get('attr')}: signal must be logs, metrics or traces")
    if errs:
        raise PlanError("; ".join(errs))
    return warns


def tap_signal(t: Dict) -> str:
    return {"prometheus": "metrics", "loki": "logs"}.get(t["kind"], t["signal"])


def tap_target(t: Dict, entry: str) -> str:
    if t["kind"] == "prometheus":
        return f"{PROM_BRIDGE}.receiver"
    if t["kind"] == "loki":
        return f"{LOKI_BRIDGE}.receiver"
    return f"{entry}.input"


# ---------------------------------------------------------------------------
# The managed Cardinal block
# ---------------------------------------------------------------------------

def _output(targets: Dict[str, str], indent: str = "  ") -> str:
    width = max(len(s) for s in targets)
    lines = [f"{indent}  {s:<{width}} = [{targets[s]}]" for s in ac.SIGNALS if s in targets]
    return f"{indent}output {{\n" + "\n".join(lines) + f"\n{indent}}}"


def cardinal_block(plan: Dict, signals: List[str], kinds: List[str]) -> str:
    literal = plan["values"] == "literal"
    entry = K8S if plan.get("k8sattributes") else TRANSFORM
    cluster_stmt = (f'`set(attributes["k8s.cluster.name"], "{plan["cluster"]}")`' if literal else
                    '"set(attributes[\\"k8s.cluster.name\\"], \\"" + sys.env("K8S_CLUSTER_NAME") + "\\")"')
    parts = [
        f"{BEGIN} — managed block; re-run /cardinal:onboard-alloy to change it\n"
        "// Sends a copy of this Alloy's telemetry to Cardinal Data Lake under\n"
        f"// otel-raw/{plan['org_id']}/{plan['cluster']}/. Nothing in this block feeds any other exporter.\n"
        f"// Requires Alloy to run with --stability.level={STABILITY_LEVEL}.",
    ]
    if "prometheus" in kinds:
        parts.append(f'otelcol.receiver.prometheus "{C}" {{\n{_output({"metrics": entry + ".input"})}\n}}')
    if "loki" in kinds:
        parts.append(f'otelcol.receiver.loki "{C}" {{\n{_output({"logs": entry + ".input"})}\n}}')
    if plan.get("k8sattributes"):
        meta = "".join(f'      "{m}",\n' for m in K8S_METADATA)
        parts.append(
            f'otelcol.processor.k8sattributes "{C}" {{\n'
            f"  extract {{\n    metadata = [\n{meta}    ]\n  }}\n"
            f"{_output({s: TRANSFORM + '.input' for s in signals})}\n}}"
        )
    stmts = []
    for s, ctx in (("metrics", "metric"), ("logs", "log"), ("traces", "trace")):
        if s in signals:
            stmts.append(f'  {ctx}_statements {{\n    context    = "resource"\n'
                         f"    statements = [{cluster_stmt}]\n  }}")
    after_transform = {s: (DELTA if s == "metrics" else BATCH) + ".input" for s in signals}
    parts.append(f'otelcol.processor.transform "{C}" {{\n  error_mode = "ignore"\n'
                 + "\n".join(stmts) + f"\n{_output(after_transform)}\n}}")
    if "metrics" in signals:
        parts.append(f'otelcol.processor.cumulativetodelta "{C}" {{\n'
                     f'  max_staleness = "{plan.get("delta_max_staleness", "15m")}"\n'
                     f"{_output({'metrics': BATCH + '.input'})}\n}}")
    b = plan["batch"]
    parts.append(f'otelcol.processor.batch "{C}" {{\n'
                 f"  send_batch_size     = {b['send_batch_size']}\n"
                 f"  send_batch_max_size = {b['send_batch_max_size']}\n"
                 f"  timeout             = \"{b['timeout']}\"\n"
                 f"{_output({s: S3 + '.input' for s in signals})}\n}}")
    if literal:
        region, bucket = f'"{plan["region"]}"', f'"{plan["bucket"]}"'
        prefix = f'"otel-raw/{plan["org_id"]}/{plan["cluster"]}"'
    else:
        region, bucket = 'sys.env("AWS_REGION")', 'sys.env("AWS_S3_BUCKET")'
        prefix = '"otel-raw/" + sys.env("LAKERUNNER_ORGANIZATION_ID") + "/" + sys.env("K8S_CLUSTER_NAME")'
    endpoint = f'    endpoint            = "{plan["endpoint"]}"\n' if plan.get("endpoint") else ""
    parts.append(f'otelcol.exporter.awss3 "{C}" {{\n'
                 f'  marshaler {{\n    type = "otlp_proto"\n  }}\n'
                 f"  s3_uploader {{\n"
                 f"    region              = {region}\n"
                 f"    s3_bucket           = {bucket}\n"
                 f"    s3_prefix           = {prefix}\n"
                 f"{endpoint}"
                 f"    s3_force_path_style = true\n"
                 f'    compression         = "gzip"\n'
                 f"  }}\n"
                 # Explicit, not defaulted: this component is experimental and its defaults
                 # may change. When S3 is slow or down, the queue fills and then drops data
                 # for Cardinal only. It never pushes back into the pipelines feeding Grafana.
                 f"  sending_queue {{\n"
                 f"    enabled           = true\n"
                 f"    block_on_overflow = false\n"
                 f"    wait_for_result   = false\n"
                 f"  }}\n}}")
    parts.append(END)
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def strip_cardinal(src: str) -> Tuple[str, bool]:
    """Remove a previous run's managed block and its taps. Returns (src, was_patched)."""
    b, e = BEGIN_RE.search(src), END_RE.search(src)
    if not b and not e:
        _check_no_managed(src)
        return src, False
    if not (b and e) or e.start() < b.start():
        raise PlanError("found only one of the Cardinal managed-block markers; fix the config by hand")
    start = b.start()
    while start > 0 and src[start - 1] == "\n" and (start < 2 or src[start - 2] == "\n"):
        start -= 1
    src = src[:start] + src[e.end():]
    _check_no_managed(src)
    g = ac.graph(ac.parse(src))
    ids = list(g.nodes)
    cuts: List[Tuple[int, int]] = []
    for cid, blk in g.nodes.items():
        for _, attr in ac.edge_lists(blk):
            lst = ac.ref_list(attr)
            if lst is None:
                continue
            for i, (ref, first, last) in enumerate(lst.refs):
                # Only the taps into the block just removed: a managed reference that no
                # longer resolves. Anything that still resolves belongs to the customer.
                if ac.is_managed_ref(ref) and ac.resolve(ref, ids) is None:
                    cuts.append(_ref_span(src, lst, i))
    for s, t in sorted(cuts, reverse=True):
        src = src[:s] + src[t:]
    return src, True


def _check_no_managed(src: str) -> None:
    """Outside the managed block, no component may use the skill's label."""
    clash = [c for c in ac.graph(ac.parse(src)).nodes if ac.is_managed(c)]
    if clash:
        raise PlanError(f"{', '.join(clash)} use{'s' if len(clash) == 1 else ''} the label "
                        f"\"{ac.MANAGED_LABEL}\" outside the managed block; this skill reserves that "
                        "label. Rename it in the customer's config first")


def _ref_span(src: str, lst: ac.ListExpr, i: int) -> Tuple[int, int]:
    ref, first, last = lst.refs[i]
    multiline = "\n" in src[lst.open_tok.end:lst.close_tok.start]
    if multiline:
        ls = src.rfind("\n", 0, first.start) + 1
        le = src.find("\n", last.end)
        line = src[ls:le]
        if line.strip().rstrip(",") == ref:
            return ls, le + 1
    if i > 0:
        return lst.refs[i - 1][2].end, last.end
    if len(lst.refs) > 1:
        return first.start, lst.refs[1][1].start
    return first.start, last.end


def patch(src: str, plan: Dict) -> Tuple[str, List[Dict]]:
    g = ac.graph(ac.parse(src))
    entry = K8S if plan.get("k8sattributes") else TRANSFORM
    edits: List[Tuple[int, str]] = []
    applied: List[Dict] = []
    for t in plan["taps"]:
        if not t.get("enabled"):
            continue
        attr = inv.find_list(g, t["component"], t["attr"])
        lst = ac.ref_list(attr) if attr else None
        if lst is None:
            raise PlanError(f"tap {t['component']}.{t['attr']} doesn't match a reference list in this "
                            "config (was the config changed since the inventory?)")
        target = tap_target(t, entry)
        if any(r == target for r, _, _ in lst.refs):
            continue
        edits.extend(ac.list_insertion(src, lst, target))
        applied.append({**t, "target": target})
    return ac.apply_insertions(src, edits), applied


def aws_partition(region: str) -> str:
    """ARN partition for a region: GovCloud, China and ISO buckets aren't `arn:aws:`."""
    for prefix, partition in (("us-gov-", "aws-us-gov"), ("cn-", "aws-cn"),
                              ("us-isob-", "aws-iso-b"), ("us-iso-", "aws-iso")):
        if region.startswith(prefix):
            return partition
    return "aws"


def iam_policy(plan: Dict) -> Dict:
    bucket = plan.get("bucket") or "<bucket>"
    partition = aws_partition(plan.get("region") or "")
    stmts = [{
        "Sid": "CardinalOtelRawWrite",
        "Effect": "Allow",
        "Action": ["s3:PutObject"],
        "Resource": f"arn:{partition}:s3:::{bucket}/otel-raw/{plan['org_id']}/{plan['cluster']}/*",
    }]
    if plan.get("kms_key_arn"):
        stmts.append({"Sid": "CardinalBucketKms", "Effect": "Allow",
                      "Action": ["kms:GenerateDataKey"], "Resource": plan["kms_key_arn"]})
    return {"Version": "2012-10-17", "Statement": stmts}


def render(src: str, plan: Dict) -> Dict:
    """Pure: returns every output as data. Raises PlanError on bad input."""
    warnings = validate_plan(plan)
    base, updated = strip_cardinal(src)
    patched, applied = patch(base, plan)
    enabled = [t for t in plan["taps"] if t.get("enabled")]
    signals = [s for s in ac.SIGNALS if s in {tap_signal(t) for t in enabled}]
    kinds = sorted({t["kind"] for t in enabled})
    block = cardinal_block(plan, signals, kinds)
    config = patched.rstrip("\n") + "\n\n" + block
    findings = lint_config.lint(ac.graph(ac.parse(config)), ac.graph(ac.parse(base)), plan)
    env = {} if plan["values"] == "literal" else {
        "LAKERUNNER_ORGANIZATION_ID": plan["org_id"],
        "K8S_CLUSTER_NAME": plan["cluster"],
        "AWS_REGION": plan.get("region") or "<region>",
        "AWS_S3_BUCKET": plan.get("bucket") or "<bucket>",
    }
    diff = "".join(difflib.unified_diff(src.splitlines(True), config.splitlines(True),
                                        "a/config.alloy", "b/config.alloy"))
    return {
        "config": config, "block": block, "diff": ac.mask_secrets(diff), "env": env,
        "iam": iam_policy(plan), "applied": applied, "signals": signals, "updated": updated,
        "warnings": warnings, "findings": findings,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", help="values file from onboard_env.py --init")
    ap.add_argument("--config", help="Alloy config file (default: ALLOY_CONFIG from --env-file)")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    values: Dict[str, str] = {}
    if args.env_file:
        values, env_cfg = onboard_env.load_checked(args.env_file)
        args.config = args.config or env_cfg
    if not args.config:
        ap.error("--config or --env-file is required")
    try:
        with open(args.config, encoding="utf-8") as f:
            src = f.read()
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
        if values:
            plan = onboard_env.apply_to_plan(plan, values)
        r = render(src, plan)
    except (OSError, ValueError, ac.AlloySyntaxError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for w in r["warnings"]:
        print(f"WARN  {w}")
    for x in r["findings"]:
        print(x)
    errors = [x for x in r["findings"] if x.severity == "error"]
    if errors:
        print(f"RESULT: FAIL — {len(errors)} lint error(s); nothing written")
        return 1

    os.makedirs(args.out, exist_ok=True)
    outputs = {
        "config.alloy": r["config"], "cardinal.alloy": r["block"], "changes.review.diff": r["diff"],
        "env.json": json.dumps(r["env"], indent=2) + "\n",
        "iam-policy.json": json.dumps(r["iam"], indent=2) + "\n",
        "render.json": json.dumps({
            "plan_version": plan.get("version"), "block_version": VERSION, "updated": r["updated"],
            "signals": r["signals"], "taps_applied": r["applied"],
            "alloy": {"stability_level": STABILITY_LEVEL, "tested_version": TESTED_ALLOY},
            "findings": [vars(x) for x in r["findings"]], "warnings": r["warnings"],
        }, indent=2) + "\n",
    }
    for name, text in outputs.items():
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        if name == "config.alloy":
            os.chmod(path, 0o600)   # it's the customer's config: may hold secrets
    print(f"{'updated' if r['updated'] else 'rendered'} Cardinal branch: signals {', '.join(r['signals'])}; "
          f"{len(r['applied'])} tap(s) added")
    for t in r["applied"]:
        print(f"  {t['component']}.{t['attr']}  += {t['target']}")
    print(f"REQUIRES  Alloy must run with --stability.level={STABILITY_LEVEL} (tested on {TESTED_ALLOY}). "
          "Roll that flag out BEFORE this config: without it Alloy rejects the whole config, "
          "Grafana pipelines included.")
    print(f"RESULT: PASS — wrote {', '.join(outputs)} to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
