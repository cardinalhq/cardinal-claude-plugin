#!/usr/bin/env python3
"""Minimal reader for Grafana Alloy configuration syntax, plus the pipeline graph.

Parses just enough of the Alloy syntax to (a) find every component and the data
edges between them (`forward_to` lists and `output { logs/metrics/traces }` lists)
and (b) patch those lists in place without touching anything else in the file.
Attribute values are kept as raw source text; nothing is evaluated.

Library only — used by alloy_inventory.py, render.py and lint_config.py.
Standard library only; Python 3.9+.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SIGNALS = ("logs", "metrics", "traces")

# Components that end a pipeline (send data out of Alloy). Anything else with no
# outgoing data edges is reported as a dangling component, not a sink.
SINK_TYPES = (
    "otelcol.exporter.otlp",
    "otelcol.exporter.otlphttp",
    "otelcol.exporter.awss3",
    "otelcol.exporter.loadbalancing",
    "otelcol.exporter.kafka",
    "otelcol.exporter.debug",
    "otelcol.exporter.logging",
    "otelcol.exporter.datadog",
    "otelcol.exporter.googlecloud",
    "otelcol.exporter.splunkhec",
    "otelcol.exporter.syslog",
    "prometheus.remote_write",
    "loki.write",
    "loki.echo",
    "pyroscope.write",
)

# Converters: exporters that hand data to another Alloy component instead of the
# network. OTLP -> Prometheus/Loki; tapping *before* them avoids a lossy
# OTLP -> Prometheus -> OTLP round trip.
CONVERTER_TYPES = {
    "otelcol.exporter.prometheus": "metrics",
    "otelcol.exporter.loki": "logs",
}

# Blocks that make the component graph incomplete from a single file's view.
OPAQUE_BLOCKS = ("declare", "import.file", "import.git", "import.http", "import.string", "remotecfg")

# Label of every component the skill adds. Deliberately not plain "cardinal":
# customers name their own components that (e.g. an OTLP receiver fed by a Cardinal
# collector), and those must never be mistaken for the skill's own.
MANAGED_LABEL = "cardinal_onboard"


def is_managed(cid: str) -> bool:
    """True for a component id the skill owns, e.g. `otelcol.exporter.awss3.cardinal_onboard`."""
    return cid.endswith("." + MANAGED_LABEL)


def is_managed_ref(ref: str) -> bool:
    """True for a reference to a managed component's export, e.g. `...batch.cardinal_onboard.input`."""
    return ("." + MANAGED_LABEL + ".") in ref


class AlloySyntaxError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

@dataclass
class Tok:
    kind: str   # ident | string | number | punct | op
    text: str
    start: int
    end: int
    line: int


_PUNCT = set("{}[](),=.:")
_OPS = ("==", "!=", "<=", ">=", "&&", "||", "+", "-", "*", "/", "%", "^", "<", ">", "!")


def tokenize(src: str) -> List[Tok]:
    toks: List[Tok] = []
    i, line, n = 0, 1, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif src.startswith("//", i) or c == "#":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise AlloySyntaxError(f"line {line}: unterminated block comment")
            line += src.count("\n", i, j)
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    raise AlloySyntaxError(f"line {line}: unterminated string")
                j += 1
            if j >= n:
                raise AlloySyntaxError(f"line {line}: unterminated string")
            toks.append(Tok("string", src[i:j + 1], i, j + 1, line))
            i = j + 1
        elif c == "`":
            j = src.find("`", i + 1)
            if j < 0:
                raise AlloySyntaxError(f"line {line}: unterminated raw string")
            toks.append(Tok("string", src[i:j + 1], i, j + 1, line))
            line += src.count("\n", i, j)
            i = j + 1
        elif c.isalpha() or c == "_":
            m = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(src, i)
            toks.append(Tok("ident", m.group(), i, m.end(), line))
            i = m.end()
        elif c.isdigit():
            m = re.compile(r"\d+(\.\d+)?([eE][+-]?\d+)?").match(src, i)
            toks.append(Tok("number", m.group(), i, m.end(), line))
            i = m.end()
        else:
            op = next((o for o in _OPS if src.startswith(o, i)), None)
            if op is not None:
                toks.append(Tok("op", op, i, i + len(op), line))
                i += len(op)
            elif c in _PUNCT:
                toks.append(Tok("punct", c, i, i + 1, line))
                i += 1
            else:
                raise AlloySyntaxError(f"line {line}: unexpected character {c!r}")
    return toks


# ---------------------------------------------------------------------------
# Parse tree
# ---------------------------------------------------------------------------

@dataclass
class Attr:
    name: str
    toks: List[Tok]          # the value expression
    start: int               # offset of the attribute name
    end: int                 # offset just past the value

    def text(self, src: str) -> str:
        return src[self.toks[0].start:self.toks[-1].end] if self.toks else ""


@dataclass
class Block:
    name: str                # e.g. "otelcol.processor.batch"
    label: Optional[str]     # e.g. "default" (unquoted), or None
    start: int
    end: int                 # offset just past the closing brace
    body_start: int          # offset just past the opening brace
    attrs: List[Attr] = field(default_factory=list)
    blocks: List["Block"] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.name}.{self.label}" if self.label else self.name

    def attr(self, name: str) -> Optional[Attr]:
        return next((a for a in self.attrs if a.name == name), None)

    def block(self, name: str) -> Optional["Block"]:
        return next((b for b in self.blocks if b.name == name), None)


class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0

    def peek(self, k: int = 0) -> Optional[Tok]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def take(self) -> Tok:
        t = self.peek()
        if t is None:
            raise AlloySyntaxError("unexpected end of file")
        self.i += 1
        return t

    def expect(self, text: str) -> Tok:
        t = self.take()
        if t.text != text:
            raise AlloySyntaxError(f"line {t.line}: expected {text!r}, got {t.text!r}")
        return t

    def dotted_ident(self) -> Tuple[str, Tok]:
        first = self.take()
        if first.kind != "ident":
            raise AlloySyntaxError(f"line {first.line}: expected a name, got {first.text!r}")
        parts = [first.text]
        while self.peek() and self.peek().text == "." and self.peek(1) and self.peek(1).kind == "ident":
            self.i += 1
            parts.append(self.take().text)
        return ".".join(parts), first

    def body(self, closing: Optional[str]) -> Tuple[List[Attr], List[Block]]:
        attrs: List[Attr] = []
        blocks: List[Block] = []
        while True:
            t = self.peek()
            if t is None:
                if closing:
                    raise AlloySyntaxError("unexpected end of file: missing '}'")
                return attrs, blocks
            if closing and t.text == closing:
                return attrs, blocks
            name, first = self.dotted_ident()
            nxt = self.peek()
            if nxt is not None and nxt.text == "=":
                self.i += 1
                val = self.expression()
                attrs.append(Attr(name, val, first.start, val[-1].end if val else nxt.end))
            else:
                label = None
                if nxt is not None and nxt.kind == "string":
                    label = self.take().text[1:-1]
                ob = self.expect("{")
                a, b = self.body("}")
                cb = self.expect("}")
                blk = Block(name, label, first.start, cb.end, ob.end, a, b)
                blocks.append(blk)

    def expression(self) -> List[Tok]:
        """Tokens of one attribute value: ends at a newline at bracket depth 0
        (unless the line ends in a binary operator), or at a closing brace."""
        out: List[Tok] = []
        depth = 0
        while True:
            t = self.peek()
            if t is None:
                break
            if depth == 0 and out:
                if t.text in ("}",) or (t.line > out[-1].line and out[-1].kind != "op"):
                    break
            if depth == 0 and t.text == "}":
                break
            if t.text in "([{" and t.kind == "punct":
                depth += 1
            elif t.text in ")]}" and t.kind == "punct":
                depth -= 1
            out.append(self.take())
        if not out:
            t = self.peek()
            raise AlloySyntaxError(f"line {t.line if t else '?'}: missing attribute value")
        return out


@dataclass
class Config:
    src: str
    blocks: List[Block]

    def components(self) -> Dict[str, Block]:
        return {b.id: b for b in self.blocks if "." in b.name and b.name not in OPAQUE_BLOCKS}

    def opaque(self) -> List[Block]:
        return [b for b in self.blocks if b.name in OPAQUE_BLOCKS]


def parse(src: str) -> Config:
    p = _Parser(src)
    attrs, blocks = p.body(None)
    if attrs:
        raise AlloySyntaxError(f"top-level attribute {attrs[0].name!r} is not valid Alloy config")
    return Config(src, blocks)


# ---------------------------------------------------------------------------
# References and data edges
# ---------------------------------------------------------------------------

@dataclass
class ListExpr:
    """An array literal whose elements are all references (a data-edge list)."""
    attr: Attr
    refs: List[Tuple[str, Tok, Tok]]   # (dotted ref, first tok, last tok)
    open_tok: Tok
    close_tok: Tok


def ref_list(attr: Attr) -> Optional[ListExpr]:
    """Parse `[a.b.c.input, d.e.receiver]`. None if the value isn't such a list."""
    toks = attr.toks
    if not toks or toks[0].text != "[" or toks[-1].text != "]":
        return None
    refs: List[Tuple[str, Tok, Tok]] = []
    i = 1
    while i < len(toks) - 1:
        if toks[i].kind != "ident":
            return None
        parts, first = [toks[i].text], toks[i]
        i += 1
        while i < len(toks) - 1 and toks[i].text == "." and toks[i + 1].kind == "ident":
            parts.append(toks[i + 1].text)
            i += 2
        refs.append((".".join(parts), first, toks[i - 1]))
        if i < len(toks) - 1:
            if toks[i].text != ",":
                return None
            i += 1
    return ListExpr(attr, refs, toks[0], toks[-1])


def resolve(ref: str, ids: List[str]) -> Optional[str]:
    """Map `otelcol.processor.batch.default.input` to `otelcol.processor.batch.default`."""
    best = None
    for cid in ids:
        if ref.startswith(cid + ".") and (best is None or len(cid) > len(best)):
            best = cid
    return best


@dataclass
class Edge:
    src: str           # component id
    dst: str           # component id
    signal: str        # logs | metrics | traces
    ref: str           # the raw reference text
    lst: ListExpr      # where the reference lives (for patching)


def component_signal(block: Block) -> Optional[str]:
    """Signal carried by a Prometheus/Loki-style component's forward_to."""
    if block.name.startswith("prometheus.") or block.name in ("otelcol.exporter.prometheus",):
        return "metrics"
    if block.name.startswith("loki.") or block.name == "otelcol.exporter.loki":
        return "logs"
    if block.name.startswith("pyroscope."):
        return "profiles"
    return None


def edge_lists(block: Block) -> List[Tuple[str, Attr]]:
    """(signal, attr) for every data-edge list inside a component block."""
    out: List[Tuple[str, Attr]] = []
    fwd = block.attr("forward_to")
    if fwd is not None:
        out.append((component_signal(block) or "unknown", fwd))
    outp = block.block("output")
    if outp is not None:
        for a in outp.attrs:
            if a.name in SIGNALS:
                out.append((a.name, a))
    return out


@dataclass
class Graph:
    config: Config
    nodes: Dict[str, Block]
    edges: List[Edge]
    unresolved: List[Tuple[str, str]]      # (component id, ref)
    unparsable: List[Tuple[str, str]]      # (component id, attr name) — non-literal edge lists

    def out_edges(self, cid: str, signal: Optional[str] = None) -> List[Edge]:
        return [e for e in self.edges if e.src == cid and (signal is None or e.signal == signal)]

    def in_edges(self, cid: str, signal: Optional[str] = None) -> List[Edge]:
        return [e for e in self.edges if e.dst == cid and (signal is None or e.signal == signal)]

    def sinks(self) -> List[str]:
        return [cid for cid, b in self.nodes.items() if b.name in SINK_TYPES]

    def upstream(self, cid: str) -> List[str]:
        seen, stack = set(), [cid]
        while stack:
            cur = stack.pop()
            for e in self.in_edges(cur):
                if e.src not in seen:
                    seen.add(e.src)
                    stack.append(e.src)
        return sorted(seen)

    def downstream(self, cid: str) -> List[str]:
        seen, stack = set(), [cid]
        while stack:
            cur = stack.pop()
            for e in self.out_edges(cur):
                if e.dst not in seen:
                    seen.add(e.dst)
                    stack.append(e.dst)
        return sorted(seen)


def graph(config: Config) -> Graph:
    nodes = config.components()
    ids = list(nodes)
    edges: List[Edge] = []
    unresolved: List[Tuple[str, str]] = []
    unparsable: List[Tuple[str, str]] = []
    for cid, blk in nodes.items():
        for signal, attr in edge_lists(blk):
            lst = ref_list(attr)
            if lst is None:
                unparsable.append((cid, attr.name))
                continue
            for ref, _, _ in lst.refs:
                dst = resolve(ref, ids)
                if dst is None:
                    unresolved.append((cid, ref))
                else:
                    edges.append(Edge(cid, dst, signal, ref, lst))
    return Graph(config, nodes, edges, unresolved, unparsable)


def load(path: str) -> Graph:
    with open(path, encoding="utf-8") as f:
        return graph(parse(f.read()))


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def list_insertion(src: str, lst: ListExpr, ref: str) -> List[Tuple[int, str]]:
    """(offset, text) edits that append `ref` to the list, matching its layout.

    A trailing comment on the last element stays on that element's line.
    """
    close = lst.close_tok.start
    if not lst.refs:
        return [(close, ref)]
    last_end = lst.refs[-1][2].end
    between = src[last_end:close]
    multiline = "\n" in src[lst.open_tok.end:close]
    if not multiline:
        return [(last_end, f", {ref}")]
    line_start = src.rfind("\n", 0, lst.refs[-1][1].start) + 1
    indent = re.match(r"[ \t]*", src[line_start:]).group()
    # The tokenizer skips comments, so a comma inside `// a, b` isn't mistaken for the list's.
    comma = next((t for t in tokenize(between) if t.text == ","), None)
    after = last_end + comma.end if comma else last_end
    eol = src.find("\n", after, close)
    line = f"\n{indent}{ref},"
    if eol < 0:
        return [(after, line if comma else "," + line)]
    return ([] if comma else [(last_end, ",")]) + [(eol, line)]


def apply_insertions(src: str, edits: List[Tuple[int, str]]) -> str:
    for off, text in sorted(edits, key=lambda e: e[0], reverse=True):
        src = src[:off] + text + src[off:]
    return src


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_SECRET_NAMES = {
    "password", "passwd", "token", "api_key", "apikey", "secret", "secret_key", "access_key",
    "secret_access_key", "session_token", "bearer_token", "client_secret", "key", "key_pem",
    "private_key", "credentials", "authorization", "proxy-authorization", "x-api-key", "api-key",
}
_STRING = r'"(?:[^"\\\n]|\\.)*"|`[^`]*`'
# `name = "literal"` or `"Header-Name" = "literal"`, anywhere on a line (also one-line
# blocks). Only literal values are redacted: sys.env(...), local.file... are references.
_SECRET_ATTR = re.compile(r'(?<![\w.-])("?)([A-Za-z_][\w-]*)\1(\s*=\s*)(' + _STRING + ")")
_AUTH_VALUE = re.compile(r'"(Bearer|Basic)\s+(?:[^"\\\n]|\\.)+"', re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """Redact secret-looking literal values for display."""
    def attr(m: "re.Match[str]") -> str:
        if m.group(2).lower() not in _SECRET_NAMES:
            return m.group(0)
        return f'{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}"<redacted>"'
    text = _SECRET_ATTR.sub(attr, text)
    # Auth header values under any key, e.g. header { key = "Authorization" value = "Bearer …" }.
    return _AUTH_VALUE.sub(lambda m: f'"{m.group(1)} <redacted>"', text)
