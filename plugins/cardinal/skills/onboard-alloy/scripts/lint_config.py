#!/usr/bin/env python3
"""Check an Alloy config's Cardinal branch against the rules that keep Grafana safe
and Cardinal ingestion correct.

Offline. Given the patched config (and ideally the original and the plan), reports
findings and exits 1 if any is an error. render.py runs this on its own output.

Usage:
  lint_config.py --config out/config.alloy [--original config.alloy] [--plan plan.json]

Rules (error unless noted):
  C001  a Cardinal component feeds a non-Cardinal component (Cardinal data could reach Grafana)
  C002  delta conversion that wasn't in the original is upstream of a non-Cardinal exporter
  C003  otelcol.exporter.awss3 is fed by something other than a batch processor
  C004  the batch feeding awss3 doesn't use the plan's settings
  C005  awss3 marshaler isn't otlp_proto, or compression isn't gzip
  C006  awss3 overrides file_prefix (Cardinal routes files by their logs_/metrics_/traces_ names)
  C007  awss3 s3_prefix isn't otel-raw/<org>/<cluster> as planned
  C008  metrics reach the Cardinal batch without passing cumulativetodelta
  C009  the customer's existing components changed beyond added Cardinal targets
  C010  a Cardinal component has no inbound data (dead branch)                  (warn)
  C011  graph incomplete: modules, unresolved references, non-literal lists    (warn)
  C012  more than one enabled tap per signal feeds different exporters         (warn)
  C013  awss3 sending_queue missing, disabled, or blocking (S3 trouble would push back into Grafana's pipelines)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alloy_config as ac  # noqa: E402



@dataclass
class Finding:
    rule: str
    severity: str   # error | warn
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper():<5} {self.rule}  {self.message}"


is_cardinal = ac.is_managed


def _attr_text(src: str, blk: ac.Block, path: List[str]) -> Optional[str]:
    cur: Optional[ac.Block] = blk
    for name in path[:-1]:
        cur = cur.block(name) if cur else None
    if cur is None:
        return None
    a = cur.attr(path[-1])
    return a.text(src) if a else None


def _unquote(s: Optional[str]) -> Optional[str]:
    if s and len(s) >= 2 and s[0] == s[-1] and s[0] in "\"`":
        return s[1:-1]
    return s


def expected_prefix_exprs(plan: Dict) -> List[str]:
    if plan.get("values") == "literal":
        return [f'"otel-raw/{plan["org_id"]}/{plan["cluster"]}"']
    return ['"otel-raw/" + sys.env("LAKERUNNER_ORGANIZATION_ID") + "/" + sys.env("K8S_CLUSTER_NAME")']


def _norm(toks: List[ac.Tok]) -> List[str]:
    return [t.text for t in toks]


def compare_blocks(orig: ac.Block, new: ac.Block, new_ids: List[str], where: str, out: List[Finding]) -> None:
    """C009: `new` must equal `orig` except for Cardinal refs appended to edge lists."""
    if [a.name for a in orig.attrs] != [a.name for a in new.attrs]:
        out.append(Finding("C009", "error", f"{where}: attributes added or removed"))
        return
    for oa, na in zip(orig.attrs, new.attrs):
        _compare_attr(oa, na, oa.name == "forward_to", new_ids, f"{where}.{oa.name}", out)
    if [(b.name, b.label) for b in orig.blocks] != [(b.name, b.label) for b in new.blocks]:
        out.append(Finding("C009", "error", f"{where}: nested blocks added, removed or reordered"))
        return
    for ob, nb in zip(orig.blocks, new.blocks):
        if ob.name == "output":
            for oa, na in zip(ob.attrs, nb.attrs):
                if oa.name != na.name:
                    out.append(Finding("C009", "error", f"{where}.output: attributes changed"))
                    return
                _compare_attr(oa, na, oa.name in ac.SIGNALS, new_ids, f"{where}.output.{oa.name}", out)
            if len(ob.attrs) != len(nb.attrs):
                out.append(Finding("C009", "error", f"{where}.output: attributes added or removed"))
        else:
            compare_blocks(ob, nb, new_ids, f"{where}.{ob.name}", out)


def _compare_attr(oa: ac.Attr, na: ac.Attr, is_edge: bool, new_ids: List[str], where: str, out: List[Finding]) -> None:
    if _norm(oa.toks) == _norm(na.toks):
        return
    if is_edge:
        ol, nl = ac.ref_list(oa), ac.ref_list(na)
        if ol is not None and nl is not None:
            kept = [r for r, _, _ in nl.refs if not is_cardinal(ac.resolve(r, new_ids) or "")]
            if kept == [r for r, _, _ in ol.refs]:
                return
    out.append(Finding("C009", "error", f"{where}: value changed"))


def lint(patched: ac.Graph, original: Optional[ac.Graph] = None, plan: Optional[Dict] = None) -> List[Finding]:
    f: List[Finding] = []
    src = patched.config.src
    nodes = patched.nodes
    card = [c for c in nodes if is_cardinal(c)]

    # C001 — Cardinal output must stay inside the Cardinal branch.
    for e in patched.edges:
        if is_cardinal(e.src) and not is_cardinal(e.dst):
            f.append(Finding("C001", "error", f"{e.src} sends {e.signal} to {e.dst}, which isn't part of the Cardinal branch"))

    # C002 — no new delta conversion upstream of a customer exporter.
    orig_ids = set(original.nodes) if original else set()
    for sink in patched.sinks():
        if is_cardinal(sink):
            continue
        for up in patched.upstream(sink):
            if nodes[up].name == "otelcol.processor.cumulativetodelta":
                if original is None:
                    f.append(Finding("C002", "warn", f"{up} is upstream of {sink}; pass --original to tell whether it's new"))
                elif up not in orig_ids:
                    f.append(Finding("C002", "error", f"{up} converts metrics to delta upstream of {sink} (Grafana expects cumulative)"))

    batch_cfg = (plan or {}).get("batch") or {"send_batch_size": 10000, "send_batch_max_size": 30000, "timeout": "10s"}
    for cid, blk in nodes.items():
        # Only the skill's own exporter: a customer's awss3 (e.g. an archive) is theirs to configure.
        if blk.name != "otelcol.exporter.awss3" or not is_cardinal(cid):
            continue
        # C003 / C004
        for e in patched.in_edges(cid):
            up = nodes[e.src]
            if up.name != "otelcol.processor.batch":
                f.append(Finding("C003", "error", f"{cid} receives {e.signal} from {e.src}; only a batch processor may feed it (each batch = one S3 PUT)"))
                continue
            for key, want in batch_cfg.items():
                got = _attr_text(src, up, [key])
                if got is None or _unquote(got) != str(want):
                    f.append(Finding("C004", "error", f"{e.src}.{key} is {got or 'unset'}, expected {want}"))
        if not patched.in_edges(cid):
            f.append(Finding("C003", "error", f"{cid} has no batch processor feeding it"))
        # C005
        if _unquote(_attr_text(src, blk, ["marshaler", "type"])) != "otlp_proto":
            f.append(Finding("C005", "error", f"{cid}: marshaler type must be \"otlp_proto\""))
        if _unquote(_attr_text(src, blk, ["s3_uploader", "compression"])) != "gzip":
            f.append(Finding("C005", "error", f"{cid}: s3_uploader.compression must be \"gzip\""))
        # C006
        if _attr_text(src, blk, ["s3_uploader", "file_prefix"]) is not None:
            f.append(Finding("C006", "error", f"{cid}: remove s3_uploader.file_prefix; files must keep the default logs_/metrics_/traces_ names"))
        # C013 — a non-blocking queue keeps S3 trouble inside the Cardinal branch.
        if blk.block("sending_queue") is None:
            f.append(Finding("C013", "error", f"{cid}: set sending_queue explicitly (enabled = true, block_on_overflow = false, wait_for_result = false)"))
        else:
            for key, bad in (("enabled", "false"), ("block_on_overflow", "true"), ("wait_for_result", "true")):
                if _attr_text(src, blk, ["sending_queue", key]) == bad:
                    f.append(Finding("C013", "error", f"{cid}: sending_queue.{key} = {bad} would let S3 trouble block the pipelines feeding Grafana"))
        # C007
        got = _attr_text(src, blk, ["s3_uploader", "s3_prefix"])
        if plan is not None:
            if got not in expected_prefix_exprs(plan):
                f.append(Finding("C007", "error", f"{cid}: s3_prefix is {got or 'unset'}, expected {expected_prefix_exprs(plan)[0]}"))
        elif got is None or "otel-raw/" not in got:
            f.append(Finding("C007", "error", f"{cid}: s3_prefix must start with otel-raw/<org>/<cluster>"))

    # C008 — metrics into the Cardinal batch must come from cumulativetodelta.
    for cid in card:
        if nodes[cid].name == "otelcol.processor.batch":
            for e in patched.in_edges(cid, "metrics"):
                if nodes[e.src].name != "otelcol.processor.cumulativetodelta":
                    f.append(Finding("C008", "error", f"metrics reach {cid} from {e.src} without cumulativetodelta"))

    # C009 — the customer's components are unchanged except for added Cardinal targets.
    if original is not None:
        new_ids = list(nodes)
        for cid, oblk in original.nodes.items():
            if is_cardinal(cid):
                continue
            nblk = nodes.get(cid)
            if nblk is None:
                f.append(Finding("C009", "error", f"{cid} was removed"))
                continue
            compare_blocks(oblk, nblk, new_ids, cid, f)
        orig_top = [b.id for b in original.config.blocks if not is_cardinal(b.id)]
        new_top = [b.id for b in patched.config.blocks if not is_cardinal(b.id)]
        added = [b for b in new_top if b not in orig_top]
        if added:
            f.append(Finding("C009", "error", f"non-Cardinal blocks added: {', '.join(added)}"))

    # C010 — every Cardinal component gets data.
    for cid in card:
        if not patched.in_edges(cid):
            f.append(Finding("C010", "warn", f"{cid} receives no data"))

    # C011 — graph completeness.
    for b in patched.config.opaque():
        f.append(Finding("C011", "warn", f"`{b.id}` block: its components aren't checked"))
    for cid, ref in patched.unresolved:
        sev = "error" if is_cardinal(cid) else "warn"
        f.append(Finding("C011", sev, f"{cid} references undefined {ref}"))
    for cid, attr in patched.unparsable:
        f.append(Finding("C011", "warn", f"{cid}.{attr} isn't a literal reference list; not checked"))

    # C012 — duplicate taps.
    by_signal: Dict[str, set] = {}
    for e in patched.edges:
        if is_cardinal(e.dst) and not is_cardinal(e.src):
            sig = e.signal if e.signal in ac.SIGNALS else ("metrics" if "prometheus" in e.dst else "logs")
            by_signal.setdefault(sig, set()).add(e.src)
    for sig, srcs in sorted(by_signal.items()):
        if len(srcs) > 1:
            f.append(Finding("C012", "warn", f"{sig} tapped from {len(srcs)} places ({', '.join(sorted(srcs))}); make sure they don't carry the same data"))

    if not card:
        f.append(Finding("C010", "warn", "no Cardinal components found"))
    return f


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="patched Alloy config")
    ap.add_argument("--original", help="the customer's config before patching")
    ap.add_argument("--plan", help="plan.json")
    args = ap.parse_args(argv)
    try:
        patched = ac.load(args.config)
        original = ac.load(args.original) if args.original else None
        plan = None
        if args.plan:
            with open(args.plan, encoding="utf-8") as fh:
                plan = json.load(fh)
    except (OSError, ValueError, ac.AlloySyntaxError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    findings = lint(patched, original, plan)
    for x in findings:
        print(x)
    errors = sum(1 for x in findings if x.severity == "error")
    print(f"RESULT: {'FAIL' if errors else 'PASS'} — {errors} error(s), {len(findings) - errors} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
