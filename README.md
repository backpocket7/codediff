# Critique

`cr` is a tiny local webapp for reviewing git changes side by side.

## Install

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Then run `cr` from the virtualenv:

```sh
.venv/bin/cr branch_name
```

If you activate the virtualenv first, `cr branch_name` will work directly.

## Usage

```sh
cr branch_name
cr commit_hash
cr branch1 branch2
cr commit_hash1 commit_hash2
```

With one ref, `cr` compares `git merge-base <ref> master` to `<ref>`. If the repo uses `main` instead of `master`, it falls back to `main`. With two refs, it compares them directly.

The browser view shows every affected file with its full repo path. Click a file to expand a side-by-side diff with syntax highlighting, diff colors, and inline changed spans. Click it again to fold the file.

Useful flags:

```sh
cr feature-branch --no-open
cr main feature-branch --port 9000
```

For development without activating the virtualenv, use `.venv/bin/cr ...`.
