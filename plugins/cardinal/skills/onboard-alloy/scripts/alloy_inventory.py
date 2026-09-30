#!/usr/bin/env python3
"""Inventory an Alloy config and suggest where to tap it for Cardinal.

Offline and read-only: parses the config file, prints the pipelines that end in an
exporter, and writes a suggested plan (taps) for render.py. Attribute values are
never printed, so secrets inside the config stay out of the output.

Usage:
  alloy_inventory.py --env-file .env.onboard-alloy --out onboard/<cluster>
  alloy_inventory.py --config config.alloy --out onboard/<cluster> [--org-id <uuid>] [--cluster <name>]

With --env-file (see onboard_env.py), the config path and every value come from the file.

A *tap* is one data-edge list (a `forward_to` or `output { <signal> = [...] }`)
that feeds a Grafana-bound exporter. render.py appends a Cardinal target to each
enabled tap, so Cardinal receives exactly what that exporter receives — after the
customer's own relabeling, filtering and sampling.

Writes:
  <out>/inventory.json   components, sinks, taps, warnings
  <out>/plan.json        suggested plan (edit, then approve, then render.py)

Exit codes: 0 ok, 2 bad input/parse error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alloy_config as ac  # noqa: E402
import onboard_env  # noqa: E402

# Sinks that don't carry data to Grafana: never tapped.
NON_GRAFANA_SINKS = ("otelcol.exporter.debug", "otelcol.exporter.logging", "loki.echo")
UNSUPPORTED_SINKS = {"pyroscope.write": "profiles are not ingested by Cardinal Data Lake"}

DEFAULT_BATCH = {"send_batch_size": 10000, "send_batch_max_size": 30000, "timeout": "10s"}


is_cardinal = ac.is_managed


def tap_kind(sink_type: str) -> str:
    if sink_type == "prometheus.remote_write":
        return "prometheus"
    if sink_type == "loki.write":
        return "loki"
    return "otlp"


def attr_path(attr: ac.Attr) -> str:
    return "forward_to" if attr.name == "forward_to" else f"output.{attr.name}"


def find_list(g: ac.Graph, component: str, path: str) -> Optional[ac.Attr]:
    """The edge-list attribute a tap names, e.g. ("x.y.batch.default", "output.metrics")."""
    blk = g.nodes.get(component)
    if blk is None:
        return None
    if path == "forward_to":
        return blk.attr("forward_to")
    if path.startswith("output."):
        out = blk.block("output")
        return out.attr(path.split(".", 1)[1]) if out else None
    return None


def find_taps(g: ac.Graph) -> List[Dict]:
    """One tap per (component, edge list) that feeds a Grafana-bound sink.

    Walks back through converters (otelcol.exporter.prometheus / .loki) so OTLP
    data is tapped as OTLP, not round-tripped through Prometheus/Loki formats.
    """
    taps: Dict[tuple, Dict] = {}

    def add(edge: ac.Edge, kind: str, signal: str, sink: str) -> None:
        key = (edge.src, attr_path(edge.lst.attr))
        if key not in taps:
            taps[key] = {"component": edge.src, "attr": key[1], "signal": signal,
                         "kind": kind, "sink": sink, "enabled": True}

    def walk(cid: str, sink: str, kind: str, seen: set) -> None:
        for e in g.in_edges(cid):
            if is_cardinal(e.src) or e.src in seen:
                continue
            src_type = g.nodes[e.src].name
            if src_type in ac.CONVERTER_TYPES:
                walk(e.src, sink, "otlp", seen | {e.src})
            else:
                add(e, kind, e.signal if kind == "otlp" else ("metrics" if kind == "prometheus" else "logs"), sink)

    for sink in g.sinks():
        blk = g.nodes[sink]
        if is_cardinal(sink) or blk.name in NON_GRAFANA_SINKS or blk.name in UNSUPPORTED_SINKS:
            continue
        walk(sink, sink, tap_kind(blk.name), {sink})
    return sorted(taps.values(), key=lambda t: (t["signal"], t["component"], t["attr"]))


def warnings_for(g: ac.Graph, taps: List[Dict]) -> List[str]:
    w: List[str] = []
    for b in g.config.opaque():
        w.append(f"`{b.id}` block found: components defined in modules/remote config aren't "
                 "visible here, so the inventory may be incomplete. Inspect the running Alloy's "
                 "component graph instead.")
        if b.name == "remotecfg":
            w.append("This Alloy pulls config from Fleet Management (remotecfg): local edits won't "
                     "stick. The Cardinal pipeline must be added in Fleet Management.")
    for cid, ref in g.unresolved:
        w.append(f"`{cid}` references `{ref}`, which isn't defined in this file (module or another file?).")
    for cid, attr in g.unparsable:
        w.append(f"`{cid}.{attr}` isn't a literal list of references; it can't be tapped automatically.")
    for sink in g.sinks():
        name = g.nodes[sink].name
        if name in UNSUPPORTED_SINKS:
            w.append(f"`{sink}`: {UNSUPPORTED_SINKS[name]} — not sent to Cardinal.")
    if any(is_cardinal(c) for c in g.nodes):
        w.append(f"This config already contains this skill's components (label \"{ac.MANAGED_LABEL}\"). "
                 "render.py will update them in place.")
    if any(b.name == "otelcol.exporter.awss3" and not is_cardinal(c) for c, b in g.nodes.items()):
        w.append("An otelcol.exporter.awss3 not managed by this skill exists: check it isn't "
                 "already writing to Cardinal (double ingestion).")
    by_signal: Dict[str, set] = {}
    for t in taps:
        by_signal.setdefault(t["signal"], set()).add(t["sink"])
    for sig, sinks in sorted(by_signal.items()):
        if len(sinks) > 1:
            w.append(f"{sig} go to {len(sinks)} Grafana-bound exporters ({', '.join(sorted(sinks))}). "
                     "If they carry the same data, enable only one tap or Cardinal gets duplicates.")
    for cid, b in g.nodes.items():
        if b.name == "otelcol.processor.tail_sampling":
            w.append(f"Traces are tail-sampled (`{cid}`): taps sit after it, so Cardinal gets the "
                     "same sample Grafana gets. Tail sampling needs all spans of a trace on one "
                     "instance; check this still holds in the chosen topology.")
    return w


def has_k8sattributes_upstream(g: ac.Graph, taps: List[Dict]) -> bool:
    otlp = [t for t in taps if t["kind"] == "otlp"]
    if not otlp:
        return False
    for t in otlp:
        chain = set(g.upstream(t["component"])) | {t["component"]}
        if not any(g.nodes[c].name == "otelcol.processor.k8sattributes" for c in chain):
            return False
    return True


def inventory(g: ac.Graph) -> Dict:
    taps = find_taps(g)
    return {
        "components": {cid: b.name for cid, b in sorted(g.nodes.items())},
        "sinks": {s: g.nodes[s].name for s in g.sinks()},
        "taps": taps,
        "k8sattributes_upstream": has_k8sattributes_upstream(g, taps),
        "warnings": warnings_for(g, taps),
    }


def suggested_plan(inv: Dict, org_id: Optional[str], cluster: Optional[str]) -> Dict:
    return {
        "version": 1,
        "org_id": org_id or "",
        "cluster": cluster or "",
        "values": "env",
        "bucket": "",
        "region": "",
        "endpoint": None,
        "kms_key_arn": None,
        "k8sattributes": not inv["k8sattributes_upstream"],
        "batch": dict(DEFAULT_BATCH),
        "delta_max_staleness": "15m",
        "taps": inv["taps"],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", help="values file from onboard_env.py --init")
    ap.add_argument("--config", help="Alloy config file (default: ALLOY_CONFIG from --env-file)")
    ap.add_argument("--out", required=True, help="output directory (onboard/<cluster>)")
    ap.add_argument("--org-id")
    ap.add_argument("--cluster")
    args = ap.parse_args(argv)
    values: Dict[str, str] = {}
    if args.env_file:
        values, env_cfg = onboard_env.load_checked(args.env_file)
        args.config = args.config or env_cfg
    if not args.config:
        ap.error("--config or --env-file is required")

    try:
        g = ac.load(args.config)
    except (OSError, ac.AlloySyntaxError) as e:
        print(f"error: {args.config}: {e}", file=sys.stderr)
        return 2

    inv = inventory(g)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "inventory.json"), "w") as f:
        json.dump(inv, f, indent=2)
    plan_path = os.path.join(args.out, "plan.json")
    if os.path.exists(plan_path):
        print(f"{plan_path} exists — not overwritten (delete it to regenerate the suggestion)")
    else:
        with open(plan_path, "w") as f:
            plan = suggested_plan(inv, args.org_id, args.cluster)
            if values:
                plan = onboard_env.apply_to_plan(plan, values)
            json.dump(plan, f, indent=2)

    print(f"{len(inv['components'])} components, {len(inv['sinks'])} exporters")
    for s, t in inv["sinks"].items():
        print(f"  exporter  {s}")
    print(f"{len(inv['taps'])} taps (Cardinal gets what these lists send to Grafana):")
    for t in inv["taps"]:
        print(f"  {t['signal']:<8} {t['kind']:<10} {t['component']}.{t['attr']}  → {t['sink']}")
    if not inv["taps"]:
        print("  none — no Grafana-bound pipeline found")
    print(f"k8sattributes already upstream of OTLP taps: {'yes' if inv['k8sattributes_upstream'] else 'no'}")
    for w in inv["warnings"]:
        print(f"WARN  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
