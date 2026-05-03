#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import html
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable


DEFAULT_PORT = 8765
CONTEXT_LINES = 4


class RvError(Exception):
    """User-facing error raised when git cannot produce the requested diff."""


@dataclass(frozen=True)
class GitFile:
    id: int
    status: str
    path: str
    old_path: str | None
    additions: int | None = None
    deletions: int | None = None

    @property
    def display_path(self) -> str:
        if self.old_path and self.old_path != self.path:
            return f"{self.old_path} -> {self.path}"
        return self.path


@dataclass(frozen=True)
class CompareContext:
    repo_root: str
    base_ref: str
    head_ref: str
    base_label: str
    head_label: str
    base_sha: str
    head_sha: str
    files: tuple[GitFile, ...]


def run_git(
    repo_root: str,
    args: list[str],
    *,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace").strip()
        raise RvError(stderr or f"git {' '.join(args)} failed")
    return proc


def find_repo_root(start: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RvError("rv must be run from inside a git repository.")
    return proc.stdout.strip()


def ref_exists(repo_root: str, ref: str) -> bool:
    proc = run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    return proc.returncode == 0


def choose_default_target(repo_root: str) -> str:
    for ref in ("master", "main"):
        if ref_exists(repo_root, ref):
            return ref
    raise RvError("Could not find a default comparison target. Expected `master` or `main`.")


def short_sha(repo_root: str, ref: str) -> str:
    return run_git(repo_root, ["rev-parse", "--short=12", ref]).stdout.strip()


def resolve_compare(repo_root: str, refs: list[str]) -> tuple[str, str, str, str]:
    if len(refs) == 1:
        head_ref = refs[0]
        if not ref_exists(repo_root, head_ref):
            raise RvError(f"Unknown branch or commit: `{head_ref}`")
        target = choose_default_target(repo_root)
        merge_base = run_git(repo_root, ["merge-base", head_ref, target]).stdout.strip()
        return merge_base, head_ref, f"merge-base({head_ref}, {target})", head_ref

    if len(refs) == 2:
        base_ref, head_ref = refs
        missing = [ref for ref in refs if not ref_exists(repo_root, ref)]
        if missing:
            raise RvError("Unknown branch or commit: " + ", ".join(f"`{ref}`" for ref in missing))
        return base_ref, head_ref, base_ref, head_ref

    raise RvError("Usage: rv <branch-or-commit> OR rv <base-branch-or-commit> <head-branch-or-commit>")


def decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def parse_name_status(repo_root: str, base_ref: str, head_ref: str) -> list[GitFile]:
    proc = run_git(repo_root, ["diff", "--name-status", "-M", "-z", base_ref, head_ref], text=False)
    parts = proc.stdout.split(b"\0")
    files: list[GitFile] = []
    index = 0
    file_id = 0

    while index < len(parts) and parts[index]:
        status = decode_path(parts[index])
        code = status[:1]
        index += 1

        if code in {"R", "C"}:
            if index + 1 >= len(parts):
                break
            old_path = decode_path(parts[index])
            new_path = decode_path(parts[index + 1])
            index += 2
        else:
            if index >= len(parts):
                break
            new_path = decode_path(parts[index])
            old_path = None if code == "A" else new_path
            index += 1

        if code == "D":
            path = old_path or new_path
        else:
            path = new_path

        files.append(GitFile(id=file_id, status=status, path=path, old_path=old_path))
        file_id += 1

    return files


def parse_numstat(repo_root: str, base_ref: str, head_ref: str) -> dict[str, tuple[int | None, int | None]]:
    proc = run_git(repo_root, ["diff", "--numstat", "-M", "-z", base_ref, head_ref], text=False)
    chunks = proc.stdout.split(b"\0")
    stats: dict[str, tuple[int | None, int | None]] = {}
    index = 0

    while index < len(chunks) and chunks[index]:
        first = decode_path(chunks[index])
        fields = first.split("\t")
        if len(fields) < 3:
            index += 1
            continue

        added_raw, deleted_raw, path_field = fields[0], fields[1], fields[2]
        additions = None if added_raw == "-" else int(added_raw)
        deletions = None if deleted_raw == "-" else int(deleted_raw)

        if path_field:
            stats[path_field] = (additions, deletions)
            index += 1
        else:
            if index + 2 >= len(chunks):
                break
            old_path = decode_path(chunks[index + 1])
            new_path = decode_path(chunks[index + 2])
            stats[old_path] = (additions, deletions)
            stats[new_path] = (additions, deletions)
            index += 3

    return stats


def build_context(repo_root: str, refs: list[str]) -> CompareContext:
    base_ref, head_ref, base_label, head_label = resolve_compare(repo_root, refs)
    files = parse_name_status(repo_root, base_ref, head_ref)
    stats = parse_numstat(repo_root, base_ref, head_ref)
    files_with_stats = tuple(
        GitFile(
            id=item.id,
            status=item.status,
            path=item.path,
            old_path=item.old_path,
            additions=stats.get(item.path, stats.get(item.old_path or item.path, (None, None)))[0],
            deletions=stats.get(item.path, stats.get(item.old_path or item.path, (None, None)))[1],
        )
        for item in files
    )
    return CompareContext(
        repo_root=repo_root,
        base_ref=base_ref,
        head_ref=head_ref,
        base_label=base_label,
        head_label=head_label,
        base_sha=short_sha(repo_root, base_ref),
        head_sha=short_sha(repo_root, head_ref),
        files=files_with_stats,
    )


def is_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\0" in data[:8192]:
        return True
    text_bytes = sum(1 for byte in data[:8192] if byte in b"\n\r\t\f\b" or 32 <= byte <= 126 or byte >= 128)
    return text_bytes / min(len(data), 8192) < 0.72


def read_blob(repo_root: str, ref: str, path: str | None) -> bytes | None:
    if not path:
        return None
    proc = run_git(repo_root, ["show", f"{ref}:{path}"], check=False, text=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def language_for_path(path: str) -> str:
    suffix = os.path.splitext(path)[1].lower().lstrip(".")
    name = os.path.basename(path).lower()
    mapping = {
        "py": "python",
        "js": "javascript",
        "jsx": "javascript",
        "mjs": "javascript",
        "cjs": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "html": "html",
        "htm": "html",
        "css": "css",
        "scss": "css",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "md": "markdown",
        "go": "go",
        "rs": "rust",
        "rb": "ruby",
        "php": "php",
        "java": "java",
        "kt": "kotlin",
        "c": "c",
        "h": "c",
        "cc": "cpp",
        "cpp": "cpp",
        "hpp": "cpp",
        "sh": "shell",
        "bash": "shell",
        "zsh": "shell",
        "sql": "sql",
        "toml": "toml",
        "xml": "xml",
    }
    if name in {"makefile", "dockerfile"}:
        return name
    return mapping.get(suffix, suffix or "text")


KEYWORDS = {
    "python": {
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "False",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "None",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "True",
        "try",
        "while",
        "with",
        "yield",
    },
    "javascript": {
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "from",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "let",
        "new",
        "null",
        "of",
        "return",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "undefined",
        "var",
        "void",
        "while",
        "yield",
    },
    "typescript": {
        "abstract",
        "any",
        "as",
        "async",
        "await",
        "boolean",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "declare",
        "default",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "from",
        "function",
        "if",
        "implements",
        "import",
        "interface",
        "keyof",
        "let",
        "namespace",
        "never",
        "new",
        "null",
        "number",
        "private",
        "protected",
        "public",
        "readonly",
        "return",
        "string",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "type",
        "typeof",
        "undefined",
        "unknown",
        "while",
    },
    "go": {
        "break",
        "case",
        "chan",
        "const",
        "continue",
        "default",
        "defer",
        "else",
        "fallthrough",
        "for",
        "func",
        "go",
        "goto",
        "if",
        "import",
        "interface",
        "map",
        "package",
        "range",
        "return",
        "select",
        "struct",
        "switch",
        "type",
        "var",
    },
    "rust": {
        "as",
        "async",
        "await",
        "break",
        "const",
        "continue",
        "crate",
        "dyn",
        "else",
        "enum",
        "extern",
        "false",
        "fn",
        "for",
        "if",
        "impl",
        "in",
        "let",
        "loop",
        "match",
        "mod",
        "move",
        "mut",
        "pub",
        "ref",
        "return",
        "self",
        "Self",
        "static",
        "struct",
        "super",
        "trait",
        "true",
        "type",
        "unsafe",
        "use",
        "where",
        "while",
    },
    "java": {
        "abstract",
        "assert",
        "boolean",
        "break",
        "byte",
        "case",
        "catch",
        "char",
        "class",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extends",
        "final",
        "finally",
        "float",
        "for",
        "if",
        "implements",
        "import",
        "instanceof",
        "int",
        "interface",
        "long",
        "native",
        "new",
        "null",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "short",
        "static",
        "strictfp",
        "super",
        "switch",
        "synchronized",
        "this",
        "throw",
        "throws",
        "transient",
        "try",
        "void",
        "volatile",
        "while",
    },
}

for c_like in ("c", "cpp", "php", "kotlin"):
    KEYWORDS[c_like] = KEYWORDS["java"] | {"auto", "bool", "include", "inline", "nullptr", "sizeof", "typedef", "using"}

KEYWORDS["shell"] = {
    "case",
    "do",
    "done",
    "elif",
    "else",
    "esac",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "local",
    "return",
    "select",
    "then",
    "until",
    "while",
}

KEYWORDS["sql"] = {
    "and",
    "as",
    "by",
    "case",
    "create",
    "delete",
    "desc",
    "distinct",
    "drop",
    "else",
    "end",
    "from",
    "group",
    "having",
    "insert",
    "into",
    "join",
    "left",
    "limit",
    "not",
    "null",
    "on",
    "or",
    "order",
    "right",
    "select",
    "set",
    "table",
    "then",
    "update",
    "values",
    "when",
    "where",
}


def comment_pattern(language: str) -> str:
    if language in {"python", "shell", "yaml", "toml", "makefile"}:
        return r"#[^\n]*"
    if language in {"html", "xml", "markdown"}:
        return r"<!--.*?-->"
    if language == "css":
        return r"/\*.*?\*/"
    if language == "sql":
        return r"--[^\n]*"
    return r"//[^\n]*|/\*.*?\*/"


def token_regex(language: str) -> re.Pattern[str]:
    keywords = KEYWORDS.get(language, set())
    keyword_part = r"\b(?:" + "|".join(re.escape(word) for word in sorted(keywords, key=len, reverse=True)) + r")\b" if keywords else r"(?!)"
    return re.compile(
        "|".join(
            [
                f"(?P<comment>{comment_pattern(language)})",
                r"(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)",
                r"(?P<number>\b(?:0x[\da-fA-F]+|\d+(?:\.\d+)?)\b)",
                f"(?P<keyword>{keyword_part})",
                r"(?P<function>\b[A-Za-z_$][\w$]*(?=\s*\())",
            ]
        )
    )


TOKEN_CACHE: dict[str, re.Pattern[str]] = {}


def highlight_segment(segment: str, language: str) -> str:
    if not segment:
        return ""
    pattern = TOKEN_CACHE.setdefault(language, token_regex(language))
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(segment):
        start, end = match.span()
        if start > cursor:
            parts.append(html.escape(segment[cursor:start]))
        kind = match.lastgroup or "plain"
        parts.append(f'<span class="tok-{kind}">{html.escape(segment[start:end])}</span>')
        cursor = end
    if cursor < len(segment):
        parts.append(html.escape(segment[cursor:]))
    return "".join(parts)


def normalize_ranges(ranges: Iterable[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
    clipped = sorted((max(0, start), min(limit, end)) for start, end in ranges if start < end)
    merged: list[tuple[int, int]] = []
    for start, end in clipped:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def highlight_line(line: str, language: str, ranges: Iterable[tuple[int, int]] = ()) -> str:
    normalized = normalize_ranges(ranges, len(line))
    if not normalized:
        return highlight_segment(line, language)

    pieces: list[str] = []
    cursor = 0
    for start, end in normalized:
        if start > cursor:
            pieces.append(highlight_segment(line[cursor:start], language))
        pieces.append(f"<mark>{highlight_segment(line[start:end], language)}</mark>")
        cursor = end
    if cursor < len(line):
        pieces.append(highlight_segment(line[cursor:], language))
    return "".join(pieces)


def changed_ranges(old_line: str, new_line: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    matcher = difflib.SequenceMatcher(None, old_line, new_line, autojunk=False)
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if old_start != old_end:
            old_ranges.append((old_start, old_end))
        if new_start != new_end:
            new_ranges.append((new_start, new_end))
    if not old_ranges and old_line:
        old_ranges.append((0, len(old_line)))
    if not new_ranges and new_line:
        new_ranges.append((0, len(new_line)))
    return old_ranges, new_ranges


def row(
    kind: str,
    language: str,
    old_line_no: int | None,
    new_line_no: int | None,
    old_text: str = "",
    new_text: str = "",
    old_ranges: Iterable[tuple[int, int]] = (),
    new_ranges: Iterable[tuple[int, int]] = (),
) -> dict[str, object]:
    return {
        "kind": kind,
        "oldLine": old_line_no,
        "newLine": new_line_no,
        "oldHtml": highlight_line(old_text, language, old_ranges),
        "newHtml": highlight_line(new_text, language, new_ranges),
    }


def build_rows(old_text: str, new_text: str, language: str, context_lines: int = CONTEXT_LINES) -> list[dict[str, object]]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    groups = matcher.get_grouped_opcodes(context_lines)
    rows: list[dict[str, object]] = []

    for group_index, group in enumerate(groups):
        if group_index:
            rows.append({"kind": "gap", "oldLine": None, "newLine": None, "oldHtml": "", "newHtml": ""})

        first = group[0]
        last = group[-1]
        old_start = first[1] + 1
        old_count = max(0, last[2] - first[1])
        new_start = first[3] + 1
        new_count = max(0, last[4] - first[3])
        rows.append(
            {
                "kind": "hunk",
                "oldLine": None,
                "newLine": None,
                "oldHtml": html.escape(f"@@ -{old_start},{old_count} @@"),
                "newHtml": html.escape(f"@@ +{new_start},{new_count} @@"),
            }
        )

        for tag, old_a, old_b, new_a, new_b in group:
            if tag == "equal":
                for offset, old_index in enumerate(range(old_a, old_b)):
                    new_index = new_a + offset
                    rows.append(
                        row(
                            "context",
                            language,
                            old_index + 1,
                            new_index + 1,
                            old_lines[old_index],
                            new_lines[new_index],
                        )
                    )
            elif tag == "delete":
                for old_index in range(old_a, old_b):
                    rows.append(row("delete", language, old_index + 1, None, old_lines[old_index], ""))
            elif tag == "insert":
                for new_index in range(new_a, new_b):
                    rows.append(row("insert", language, None, new_index + 1, "", new_lines[new_index]))
            elif tag == "replace":
                old_range = list(range(old_a, old_b))
                new_range = list(range(new_a, new_b))
                paired = min(len(old_range), len(new_range))
                for pair_index in range(paired):
                    old_index = old_range[pair_index]
                    new_index = new_range[pair_index]
                    old_marks, new_marks = changed_ranges(old_lines[old_index], new_lines[new_index])
                    rows.append(
                        row(
                            "change",
                            language,
                            old_index + 1,
                            new_index + 1,
                            old_lines[old_index],
                            new_lines[new_index],
                            old_marks,
                            new_marks,
                        )
                    )
                for old_index in old_range[paired:]:
                    rows.append(row("delete", language, old_index + 1, None, old_lines[old_index], ""))
                for new_index in new_range[paired:]:
                    rows.append(row("insert", language, None, new_index + 1, "", new_lines[new_index]))

    if not rows and old_text != new_text:
        rows.append({"kind": "hunk", "oldLine": None, "newLine": None, "oldHtml": "@@ changed @@", "newHtml": "@@ changed @@"})

    return rows


def file_payload(context: CompareContext, file_id: int) -> dict[str, object]:
    item = next((file for file in context.files if file.id == file_id), None)
    if item is None:
        raise RvError(f"Unknown file id: {file_id}")

    old_path = item.old_path
    new_path = None if item.status.startswith("D") else item.path
    old_blob = read_blob(context.repo_root, context.base_ref, old_path)
    new_blob = read_blob(context.repo_root, context.head_ref, new_path)
    old_blob = old_blob or b""
    new_blob = new_blob or b""
    language = language_for_path(item.path)

    payload: dict[str, object] = {
        "id": item.id,
        "path": item.path,
        "oldPath": item.old_path,
        "displayPath": item.display_path,
        "status": item.status,
        "language": language,
        "binary": False,
    }

    if is_binary(old_blob) or is_binary(new_blob):
        payload.update(
            {
                "binary": True,
                "oldSize": len(old_blob),
                "newSize": len(new_blob),
                "mimeType": mimetypes.guess_type(item.path)[0] or "application/octet-stream",
                "rows": [],
            }
        )
        return payload

    old_text = old_blob.decode("utf-8", "replace")
    new_text = new_blob.decode("utf-8", "replace")
    payload["rows"] = build_rows(old_text, new_text, language)
    return payload


def summary_payload(context: CompareContext) -> dict[str, object]:
    total_additions = sum(file.additions or 0 for file in context.files)
    total_deletions = sum(file.deletions or 0 for file in context.files)
    return {
        "repoRoot": context.repo_root,
        "baseLabel": context.base_label,
        "headLabel": context.head_label,
        "baseSha": context.base_sha,
        "headSha": context.head_sha,
        "fileCount": len(context.files),
        "additions": total_additions,
        "deletions": total_deletions,
        "files": [
            {
                "id": file.id,
                "status": file.status,
                "path": file.path,
                "oldPath": file.old_path,
                "displayPath": file.display_path,
                "absolutePath": os.path.join(context.repo_root, file.path),
                "additions": file.additions,
                "deletions": file.deletions,
            }
            for file in context.files
        ],
    }


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>rv diff viewer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --surface: #ffffff;
      --surface-2: #f0f3f6;
      --border: #d8dee7;
      --border-strong: #bac5d3;
      --text: #17202a;
      --muted: #667386;
      --blue: #245fbd;
      --blue-soft: #e8f0ff;
      --green-bg: #eaf7ed;
      --green-line: #b8e2c3;
      --red-bg: #fff0f0;
      --red-line: #f0bbbb;
      --yellow-bg: #fff7dc;
      --yellow-line: #edd27d;
      --code: #1f2937;
      --shadow: 0 10px 30px rgba(23, 32, 42, 0.08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
    }

    .app-shell {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      box-shadow: 0 1px 0 rgba(23, 32, 42, 0.02);
    }

    .header-inner {
      max-width: 1520px;
      margin: 0 auto;
      padding: 16px 22px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: center;
    }

    .brand-line {
      display: flex;
      gap: 12px;
      align-items: baseline;
      min-width: 0;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 720;
    }

    .refs {
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .summary {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }

    .metric {
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 9px;
      background: var(--surface-2);
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
    }

    .metric strong {
      color: var(--text);
      font-weight: 700;
    }

    main {
      max-width: 1520px;
      width: 100%;
      margin: 0 auto;
      padding: 18px 22px 34px;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
    }

    .filter {
      flex: 1 1 320px;
      min-width: min(100%, 240px);
      height: 36px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      color: var(--text);
      background: var(--surface);
    }

    .filter:focus {
      outline: 2px solid var(--blue-soft);
      border-color: var(--blue);
    }

    .button {
      height: 36px;
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      padding: 0 12px;
      background: var(--surface);
      color: var(--text);
      font: inherit;
      font-size: 13px;
      cursor: pointer;
    }

    .button:hover {
      border-color: var(--blue);
      color: var(--blue);
    }

    .file-list {
      display: grid;
      gap: 8px;
    }

    .file-entry {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 2px rgba(23, 32, 42, 0.03);
    }

    .file-row {
      width: 100%;
      min-height: 48px;
      padding: 0 12px;
      border: 0;
      background: transparent;
      color: inherit;
      cursor: pointer;
      display: grid;
      grid-template-columns: 22px 60px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      text-align: left;
      font: inherit;
    }

    .file-row:hover {
      background: #f7faff;
    }

    .chevron {
      color: var(--muted);
      font-size: 15px;
      transform: rotate(0deg);
      transition: transform 120ms ease;
    }

    .file-entry[data-open="true"] .chevron {
      transform: rotate(90deg);
    }

    .status {
      width: fit-content;
      min-width: 42px;
      border-radius: 999px;
      padding: 3px 8px;
      border: 1px solid var(--border);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      text-align: center;
      color: var(--muted);
      background: var(--surface-2);
    }

    .status[data-kind="A"] {
      color: #126b33;
      background: var(--green-bg);
      border-color: var(--green-line);
    }

    .status[data-kind="D"] {
      color: #a32f2f;
      background: var(--red-bg);
      border-color: var(--red-line);
    }

    .status[data-kind="M"], .status[data-kind="R"], .status[data-kind="C"] {
      color: #845600;
      background: var(--yellow-bg);
      border-color: var(--yellow-line);
    }

    .path {
      min-width: 0;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 13px;
      color: var(--code);
    }

    .file-stat {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
    }

    .plus {
      color: #147a3b;
    }

    .minus {
      color: #b13737;
    }

    .diff-panel {
      border-top: 1px solid var(--border);
      background: #fbfcfe;
    }

    .panel-state {
      padding: 20px;
      color: var(--muted);
      font-size: 13px;
    }

    .diff-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
    }

    .language {
      border-radius: 999px;
      border: 1px solid var(--border);
      padding: 3px 8px;
      background: var(--surface);
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }

    .diff-scroll {
      overflow-x: auto;
      max-height: 72vh;
    }

    .diff-grid {
      min-width: 980px;
      display: grid;
      grid-template-columns: 58px minmax(390px, 1fr) 58px minmax(390px, 1fr);
      align-items: stretch;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.52;
      color: var(--code);
    }

    .diff-grid-head {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #e9eef5;
      color: #4f5c6e;
      border-bottom: 1px solid var(--border-strong);
      font-weight: 700;
      padding: 6px 10px;
    }

    .line-no {
      user-select: none;
      text-align: right;
      color: #8793a4;
      background: #f1f4f8;
      border-right: 1px solid var(--border);
      padding: 0 8px;
      white-space: pre;
    }

    .code-cell {
      white-space: pre;
      overflow: visible;
      padding: 0 10px;
      border-right: 1px solid var(--border);
    }

    .diff-row-context.code-cell,
    .diff-row-context.line-no {
      background: #ffffff;
    }

    .diff-row-delete.old-cell,
    .diff-row-delete.old-no {
      background: var(--red-bg);
    }

    .diff-row-insert.new-cell,
    .diff-row-insert.new-no {
      background: var(--green-bg);
    }

    .diff-row-change.old-cell,
    .diff-row-change.old-no,
    .diff-row-change.new-cell,
    .diff-row-change.new-no {
      background: var(--yellow-bg);
    }

    .diff-row-hunk.code-cell,
    .diff-row-hunk.line-no,
    .diff-row-gap.code-cell,
    .diff-row-gap.line-no {
      background: #edf3fb;
      color: #53667e;
      font-weight: 700;
    }

    mark {
      color: inherit;
      border-radius: 3px;
      padding: 0 1px;
      background: rgba(255, 171, 46, 0.45);
    }

    .diff-row-delete mark {
      background: rgba(219, 67, 67, 0.22);
    }

    .diff-row-insert mark {
      background: rgba(30, 142, 62, 0.22);
    }

    .tok-keyword {
      color: #7b3fb8;
      font-weight: 700;
    }

    .tok-string {
      color: #1b6f4a;
    }

    .tok-number {
      color: #985f0d;
    }

    .tok-comment {
      color: #7a8698;
      font-style: italic;
    }

    .tok-function {
      color: #245fbd;
    }

    .empty-state {
      padding: 40px 12px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--border-strong);
      background: var(--surface);
      border-radius: 8px;
    }

    @media (max-width: 820px) {
      .header-inner {
        grid-template-columns: 1fr;
      }

      .summary {
        justify-content: flex-start;
      }

      .file-row {
        grid-template-columns: 20px 52px minmax(0, 1fr);
      }

      .file-stat {
        grid-column: 3;
        justify-self: start;
        padding-bottom: 8px;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header>
      <div class="header-inner">
        <div class="brand-line">
          <h1>rv</h1>
          <div class="refs" id="refs">Loading comparison</div>
        </div>
        <div class="summary" id="summary"></div>
      </div>
    </header>
    <main>
      <div class="toolbar">
        <input class="filter" id="filter" type="search" placeholder="Filter files by path" autocomplete="off">
        <button class="button" id="expandAll" type="button">Expand all</button>
        <button class="button" id="collapseAll" type="button">Collapse all</button>
      </div>
      <div class="file-list" id="files"></div>
    </main>
  </div>

  <script>
    const state = {
      summary: null,
      open: new Set(),
      cache: new Map(),
      filter: ""
    };

    const refsEl = document.querySelector("#refs");
    const summaryEl = document.querySelector("#summary");
    const filesEl = document.querySelector("#files");
    const filterEl = document.querySelector("#filter");

    function escapeAttr(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function statusKind(status) {
      return String(status || "").slice(0, 1);
    }

    function fileStats(file) {
      if (file.additions === null && file.deletions === null) return "binary";
      return `<span class="plus">+${file.additions ?? 0}</span> <span class="minus">-${file.deletions ?? 0}</span>`;
    }

    function renderSummary(data) {
      refsEl.textContent = `${data.baseLabel} (${data.baseSha}) -> ${data.headLabel} (${data.headSha})`;
      summaryEl.innerHTML = [
        `<span class="metric"><strong>${data.fileCount}</strong> files</span>`,
        `<span class="metric plus"><strong>+${data.additions}</strong></span>`,
        `<span class="metric minus"><strong>-${data.deletions}</strong></span>`,
        `<span class="metric" title="${escapeAttr(data.repoRoot)}">${escapeAttr(data.repoRoot)}</span>`
      ].join("");
    }

    function visibleFiles() {
      const needle = state.filter.trim().toLowerCase();
      if (!needle) return state.summary.files;
      return state.summary.files.filter(file => {
        return file.displayPath.toLowerCase().includes(needle) || file.path.toLowerCase().includes(needle);
      });
    }

    function renderFiles() {
      const files = visibleFiles();
      if (!files.length) {
        filesEl.innerHTML = `<div class="empty-state">No files match the current filter.</div>`;
        return;
      }

      filesEl.innerHTML = files.map(file => {
        const isOpen = state.open.has(file.id);
        return `
          <section class="file-entry" data-file-id="${file.id}" data-open="${isOpen ? "true" : "false"}">
            <button class="file-row" type="button" title="${escapeAttr(file.absolutePath)}" aria-expanded="${isOpen ? "true" : "false"}">
              <span class="chevron">›</span>
              <span class="status" data-kind="${statusKind(file.status)}">${escapeAttr(file.status)}</span>
              <span class="path">${escapeAttr(file.displayPath)}</span>
              <span class="file-stat">${fileStats(file)}</span>
            </button>
            ${isOpen ? `<div class="diff-panel" id="panel-${file.id}"><div class="panel-state">Loading diff</div></div>` : ""}
          </section>
        `;
      }).join("");

      for (const entry of filesEl.querySelectorAll(".file-entry")) {
        const id = Number(entry.dataset.fileId);
        entry.querySelector(".file-row").addEventListener("click", () => toggleFile(id));
        if (state.open.has(id)) {
          loadFile(id);
        }
      }
    }

    function toggleFile(id) {
      if (state.open.has(id)) {
        state.open.delete(id);
      } else {
        state.open.add(id);
      }
      renderFiles();
    }

    function rowHtml(row) {
      const cls = `diff-row-${row.kind}`;
      const oldNo = row.oldLine ?? "";
      const newNo = row.newLine ?? "";
      return `
        <div class="${cls} line-no old-no">${oldNo}</div>
        <div class="${cls} code-cell old-cell">${row.oldHtml}</div>
        <div class="${cls} line-no new-no">${newNo}</div>
        <div class="${cls} code-cell new-cell">${row.newHtml}</div>
      `;
    }

    function renderDiff(file) {
      if (file.binary) {
        return `
          <div class="diff-meta">
            <span class="language">${escapeAttr(file.mimeType)}</span>
            <span>${file.oldSize} bytes -> ${file.newSize} bytes</span>
          </div>
          <div class="panel-state">Binary file changed.</div>
        `;
      }

      if (!file.rows.length) {
        return `
          <div class="diff-meta"><span class="language">${escapeAttr(file.language)}</span></div>
          <div class="panel-state">No textual diff for this file.</div>
        `;
      }

      return `
        <div class="diff-meta">
          <span class="language">${escapeAttr(file.language)}</span>
          <span>${file.rows.length} rendered rows</span>
        </div>
        <div class="diff-scroll">
          <div class="diff-grid">
            <div class="diff-grid-head">Base</div>
            <div class="diff-grid-head">${escapeAttr(state.summary.baseLabel)}</div>
            <div class="diff-grid-head">Head</div>
            <div class="diff-grid-head">${escapeAttr(state.summary.headLabel)}</div>
            ${file.rows.map(rowHtml).join("")}
          </div>
        </div>
      `;
    }

    async function loadFile(id) {
      const panel = document.querySelector(`#panel-${id}`);
      if (!panel) return;
      if (state.cache.has(id)) {
        panel.innerHTML = renderDiff(state.cache.get(id));
        return;
      }

      try {
        const response = await fetch(`/api/file/${id}`);
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        state.cache.set(id, payload);
        const freshPanel = document.querySelector(`#panel-${id}`);
        if (freshPanel) freshPanel.innerHTML = renderDiff(payload);
      } catch (error) {
        panel.innerHTML = `<div class="panel-state">Failed to load diff: ${escapeAttr(error.message)}</div>`;
      }
    }

    filterEl.addEventListener("input", () => {
      state.filter = filterEl.value;
      renderFiles();
    });

    document.querySelector("#expandAll").addEventListener("click", () => {
      for (const file of visibleFiles()) state.open.add(file.id);
      renderFiles();
    });

    document.querySelector("#collapseAll").addEventListener("click", () => {
      state.open.clear();
      renderFiles();
    });

    async function boot() {
      try {
        const response = await fetch("/api/summary");
        if (!response.ok) throw new Error(await response.text());
        state.summary = await response.json();
        renderSummary(state.summary);
        if (!state.summary.files.length) {
          filesEl.innerHTML = `<div class="empty-state">No files changed in this comparison.</div>`;
          return;
        }
        renderFiles();
      } catch (error) {
        refsEl.textContent = "Unable to load comparison";
        filesEl.innerHTML = `<div class="empty-state">${escapeAttr(error.message)}</div>`;
      }
    }

    boot();
  </script>
</body>
</html>
"""


class RvRequestHandler(BaseHTTPRequestHandler):
    context: CompareContext

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/plain; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: object) -> None:
        self.send_text(json.dumps(payload), content_type="application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self.send_text(APP_HTML, content_type="text/html; charset=utf-8")
                return
            if path == "/api/summary":
                self.send_json(summary_payload(self.context))
                return
            if path.startswith("/api/file/"):
                raw_id = path.rsplit("/", 1)[-1]
                self.send_json(file_payload(self.context, int(raw_id)))
                return
            self.send_text("Not found", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            status = HTTPStatus.BAD_REQUEST if isinstance(exc, (ValueError, RvError)) else HTTPStatus.INTERNAL_SERVER_ERROR
            self.send_text(str(exc), status)


class RvServer(ThreadingHTTPServer):
    allow_reuse_address = True


def find_port(host: str, requested_port: int) -> int:
    for port in range(requested_port, requested_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RvError(f"No available port found between {requested_port} and {requested_port + 99}.")


def make_handler(context: CompareContext) -> type[RvRequestHandler]:
    class BoundHandler(RvRequestHandler):
        pass

    BoundHandler.context = context
    return BoundHandler


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rv",
        description="Open a local side-by-side git diff viewer.",
    )
    parser.add_argument("refs", nargs="*", help="One ref to compare against merge-base with master/main, or two refs to compare directly.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for the local web server.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Preferred server port. Defaults to {DEFAULT_PORT}.")
    parser.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        repo_root = find_repo_root(os.getcwd())
        context = build_context(repo_root, args.refs)
        port = find_port(args.host, args.port)
        server = RvServer((args.host, port), make_handler(context))
        url = f"http://{args.host}:{port}/"
        print(f"rv serving {context.base_label} -> {context.head_label}")
        print(f"{len(context.files)} files, +{sum(file.additions or 0 for file in context.files)} -{sum(file.deletions or 0 for file in context.files)}")
        print(url)
        if not args.no_open:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nrv stopped")
        finally:
            server.server_close()
        return 0
    except RvError as exc:
        print(f"rv: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
