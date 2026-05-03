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
from importlib import resources
from typing import Iterable


DEFAULT_PORT = 8765
CONTEXT_LINES = 4


class CritiqueError(Exception):
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
        raise CritiqueError(stderr or f"git {' '.join(args)} failed")
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
        raise CritiqueError("cr must be run from inside a git repository.")
    return proc.stdout.strip()


def ref_exists(repo_root: str, ref: str) -> bool:
    proc = run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    return proc.returncode == 0


def choose_default_target(repo_root: str) -> str:
    for ref in ("master", "main"):
        if ref_exists(repo_root, ref):
            return ref
    raise CritiqueError("Could not find a default comparison target. Expected `master` or `main`.")


def short_sha(repo_root: str, ref: str) -> str:
    return run_git(repo_root, ["rev-parse", "--short=12", ref]).stdout.strip()


def resolve_compare(repo_root: str, refs: list[str]) -> tuple[str, str, str, str]:
    if len(refs) == 1:
        head_ref = refs[0]
        if not ref_exists(repo_root, head_ref):
            raise CritiqueError(f"Unknown branch or commit: `{head_ref}`")
        target = choose_default_target(repo_root)
        merge_base = run_git(repo_root, ["merge-base", head_ref, target]).stdout.strip()
        return merge_base, head_ref, f"merge-base({head_ref}, {target})", head_ref

    if len(refs) == 2:
        base_ref, head_ref = refs
        missing = [ref for ref in refs if not ref_exists(repo_root, ref)]
        if missing:
            raise CritiqueError("Unknown branch or commit: " + ", ".join(f"`{ref}`" for ref in missing))
        return base_ref, head_ref, base_ref, head_ref

    raise CritiqueError("Usage: cr <branch-or-commit> OR cr <base-branch-or-commit> <head-branch-or-commit>")


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


KEYWORD_TEXT = {
    "python": "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield",
    "javascript": "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield",
    "typescript": "abstract any as async await boolean break case catch class const continue declare default else enum export extends false finally for from function if implements import interface keyof let namespace never new null number private protected public readonly return string super switch this throw true try type typeof undefined unknown while",
    "go": "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var",
    "rust": "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return Self self static struct super trait true type unsafe use where while",
    "java": "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new null package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while",
    "c": "abstract assert auto bool boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import include inline instanceof int interface long native new null nullptr package private protected public return short sizeof static strictfp super switch synchronized this throw throws transient try typedef using void volatile while",
    "cpp": "abstract assert auto bool boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import include inline instanceof int interface long native new null nullptr package private protected public return short sizeof static strictfp super switch synchronized this throw throws transient try typedef using void volatile while",
    "php": "abstract assert auto bool boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import include inline instanceof int interface long native new null nullptr package private protected public return short sizeof static strictfp super switch synchronized this throw throws transient try typedef using void volatile while",
    "kotlin": "abstract assert auto bool boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import include inline instanceof int interface long native new null nullptr package private protected public return short sizeof static strictfp super switch synchronized this throw throws transient try typedef using void volatile while",
    "shell": "case do done elif else esac fi for function if in local return select then until while",
    "sql": "and as by case create delete desc distinct drop else end from group having insert into join left limit not null on or order right select set table then update values when where",
}

KEYWORDS = {language: set(words.split()) for language, words in KEYWORD_TEXT.items()}


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
        raise CritiqueError(f"Unknown file id: {file_id}")

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


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/favicon.ico": ("codereview.png", "image/png"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/assets/codereview.png": ("codereview.png", "image/png"),
}


def read_static_asset(path: str) -> tuple[bytes, str] | None:
    asset = STATIC_FILES.get(path)
    if asset is None:
        return None
    filename, content_type = asset
    data = resources.files("critique").joinpath(filename).read_bytes()
    return data, content_type


class CritiqueRequestHandler(BaseHTTPRequestHandler):
    context: CompareContext

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/plain; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_bytes(encoded, status, content_type)

    def send_bytes(self, body: bytes, status: HTTPStatus = HTTPStatus.OK, content_type: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object) -> None:
        self.send_text(json.dumps(payload), content_type="application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            static_asset = read_static_asset(path)
            if static_asset is not None:
                body, content_type = static_asset
                self.send_bytes(body, content_type=content_type)
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
            status = HTTPStatus.BAD_REQUEST if isinstance(exc, (ValueError, CritiqueError)) else HTTPStatus.INTERNAL_SERVER_ERROR
            self.send_text(str(exc), status)


class CritiqueServer(ThreadingHTTPServer):
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
    raise CritiqueError(f"No available port found between {requested_port} and {requested_port + 99}.")


def make_handler(context: CompareContext) -> type[CritiqueRequestHandler]:
    class BoundHandler(CritiqueRequestHandler):
        pass

    BoundHandler.context = context
    return BoundHandler


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cr",
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
        server = CritiqueServer((args.host, port), make_handler(context))
        url = f"http://{args.host}:{port}/"
        print(f"Critique serving {context.base_label} -> {context.head_label}")
        print(f"{len(context.files)} files, +{sum(file.additions or 0 for file in context.files)} -{sum(file.deletions or 0 for file in context.files)}")
        print(url)
        if not args.no_open:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nCritique stopped")
        finally:
            server.server_close()
        return 0
    except CritiqueError as exc:
        print(f"cr: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
