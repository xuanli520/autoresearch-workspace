"""Minimal, dependency-free YAML subset loader for protocol.yaml.

Supported: block mappings and sequences, ``- key: value`` items, flow ``[..]``/``{..}``,
single/double quoted strings, ``|``/``>`` block scalars, comments, null/bool/int/float.
Unsupported constructs (anchors, tags, multi-document) raise ``YamlError``. JSON input is accepted.
"""
from __future__ import annotations

import json
import re

__all__ = ["YamlError", "loads"]


class YamlError(ValueError):
    pass


_INT = re.compile(r"[-+]?\d+")
_FLOAT = re.compile(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?")


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text: str):
    s = text.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if s[0] in "&*!" or s.startswith("---"):
        raise YamlError(f"unsupported YAML construct: {s!r}")
    if s[0] == '"':
        try:
            value, end = json.JSONDecoder().raw_decode(s)
        except ValueError as exc:
            raise YamlError(f"bad double-quoted string: {s!r}") from exc
        if s[end:].strip():
            raise YamlError(f"trailing text after string: {s!r}")
        return value
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'":
            raise YamlError(f"bad single-quoted string: {s!r}")
        return s[1:-1].replace("''", "'")
    if s[0] in "[{":
        value, rest = _flow(s, 0)
        if s[rest:].strip():
            raise YamlError(f"trailing text after flow value: {s!r}")
        return value
    if _INT.fullmatch(s):
        return int(s)
    if _FLOAT.fullmatch(s):
        return float(s)
    return s


def _flow(s: str, i: int):
    """Parse a flow collection starting at s[i]; return (value, next_index)."""
    opener = s[i]
    closer = "]" if opener == "[" else "}"
    i += 1
    items: list = []
    mapping: dict = {}
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s):
            raise YamlError(f"unterminated flow collection: {s!r}")
        if s[i] == closer:
            return (items if opener == "[" else mapping), i + 1
        if s[i] in "[{":
            value, i = _flow(s, i)
            token_value = value
            key_text = None
        else:
            start = i
            quote = None
            depth = 0
            while i < len(s):
                ch = s[i]
                if quote:
                    if ch == "\\" and quote == '"':
                        i += 2
                        continue
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    break
                i += 1
            token = s[start:i].strip()
            key_text = token
            token_value = None
        if opener == "{":
            if key_text is None:
                raise YamlError("flow mapping keys must be scalars")
            key, sep, val = _split_key(key_text)
            if not sep:
                raise YamlError(f"flow mapping entry needs 'key: value': {key_text!r}")
            mapping[key] = _scalar(val)
        else:
            items.append(token_value if key_text is None else _scalar(key_text))
        while i < len(s) and s[i] in " \t":
            i += 1
        if i < len(s) and s[i] == ",":
            i += 1


def _split_key(text: str):
    """Split 'key: value' respecting quotes. Returns (key, has_sep, value)."""
    if text[:1] in "\"'":
        q = text[0]
        end = text.find(q, 1)
        while q == "'" and end != -1 and text[end + 1:end + 2] == "'":
            end = text.find(q, end + 2)
        if end == -1:
            raise YamlError(f"unterminated quoted key: {text!r}")
        key = _scalar(text[:end + 1])
        rest = text[end + 1:]
        if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
            return str(key), True, rest[1:]
        return text, False, ""
    m = re.match(r"([^:]+?):(\s|$)(.*)", text)
    if not m:
        return text, False, ""
    return m.group(1).strip(), True, m.group(3)


def _flow_depth(text: str) -> int:
    depth, quote, i = 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or not text[i - 1].isalnum()):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


class _Lines:
    def __init__(self, text: str):
        self.raw = text.splitlines()
        self.items: list[tuple[int, str, int]] = []
        pending = None  # (indent, text, line_no) of an unclosed flow collection
        for no, line in enumerate(self.raw):
            if "\t" in line[: len(line) - len(line.lstrip())]:
                raise YamlError(f"line {no + 1}: tabs are not allowed for indentation")
            body = _strip_comment(line)
            if not body.strip():
                continue
            if pending is not None:
                pending = (pending[0], pending[1] + " " + body.strip(), pending[2])
            else:
                pending = (len(body) - len(body.lstrip(" ")), body.strip(), no)
            if _flow_depth(pending[1]) > 0:
                continue
            self.items.append(pending)
            pending = None
        if pending is not None:
            raise YamlError(f"line {pending[2] + 1}: unterminated flow collection")


def _block_scalar(lines: _Lines, pos: int, parent_indent: int, style: str):
    """Collect raw lines more indented than parent_indent following item pos-1."""
    start_no = lines.items[pos - 1][2] + 1
    collected: list[str] = []
    no = start_no
    block_indent = None
    while no < len(lines.raw):
        line = lines.raw[no]
        if line.strip() == "":
            collected.append("")
            no += 1
            continue
        ind = len(line) - len(line.lstrip(" "))
        if ind <= parent_indent:
            break
        if block_indent is None:
            block_indent = ind
        collected.append(line[block_indent:] if ind >= block_indent else line.lstrip(" "))
        no += 1
    while collected and collected[-1] == "":
        collected.pop()
    while pos < len(lines.items) and lines.items[pos][2] < no:
        pos += 1
    if style == "|":
        return "\n".join(collected) + "\n", pos
    return " ".join(part for part in collected if part) + "\n", pos


def _parse_block(lines: _Lines, pos: int, indent: int):
    items = lines.items
    if pos >= len(items):
        return None, pos
    first_indent, first_text, _ = items[pos]
    if first_text == "-" or first_text.startswith("- "):
        seq = []
        while pos < len(items) and items[pos][0] == indent and (items[pos][1] == "-" or items[pos][1].startswith("- ")):
            rest = items[pos][1][1:].strip()
            no = items[pos][2]
            pos += 1
            if not rest:
                if pos < len(items) and items[pos][0] > indent:
                    value, pos = _parse_block(lines, pos, items[pos][0])
                else:
                    value = None
                seq.append(value)
                continue
            key, sep, val = _split_key(rest) if rest[0] not in "[{" else (rest, False, "")
            if sep:
                child_indent = indent + 2
                lines.items.insert(pos, (child_indent, rest, no))
                value, pos = _parse_block(lines, pos, child_indent)
                seq.append(value)
            else:
                seq.append(_scalar(rest))
        return seq, pos
    mapping: dict = {}
    while pos < len(items) and items[pos][0] == indent:
        text = items[pos][1]
        if text == "-" or text.startswith("- "):
            raise YamlError(f"line {items[pos][2] + 1}: sequence item where mapping key expected")
        key, sep, val = _split_key(text)
        if not sep:
            raise YamlError(f"line {items[pos][2] + 1}: expected 'key: value', got {text!r}")
        if key in mapping:
            raise YamlError(f"line {items[pos][2] + 1}: duplicate key {key!r}")
        pos += 1
        val = val.strip()
        if val in ("|", ">", "|-", ">-"):
            value, pos = _block_scalar(lines, pos, indent, val[0])
            if val.endswith("-"):
                value = value.rstrip("\n")
        elif val:
            value = _scalar(val)
        elif pos < len(items) and items[pos][0] > indent:
            value, pos = _parse_block(lines, pos, items[pos][0])
        elif pos < len(items) and items[pos][0] == indent and (items[pos][1] == "-" or items[pos][1].startswith("- ")):
            value, pos = _parse_block(lines, pos, indent)
        else:
            value = None
        mapping[key] = value
    return mapping, pos


def loads(text: str):
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise YamlError(f"invalid JSON: {exc}") from exc
    lines = _Lines(text)
    if not lines.items:
        return None
    if lines.items[0][1].startswith("---"):
        raise YamlError("multi-document YAML is not supported")
    value, pos = _parse_block(lines, 0, lines.items[0][0])
    if pos != len(lines.items):
        _, txt, no = lines.items[pos]
        raise YamlError(f"line {no + 1}: unexpected indentation or content: {txt!r}")
    return value
