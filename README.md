# rv

`rv` is a tiny local webapp for reviewing git changes side by side.

## Install

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Then run the installed command:

```sh
.venv/bin/rv branch_name
```

If you activate the virtualenv first, `rv branch_name` will work directly.

## Usage

```sh
./rv branch_name
./rv commit_hash
./rv branch1 branch2
./rv commit_hash1 commit_hash2
```

With one ref, `rv` compares `git merge-base <ref> master` to `<ref>`. If the repo uses `main` instead of `master`, it falls back to `main`. With two refs, it compares them directly.

The browser view shows every affected file with its full repo path. Click a file to expand a side-by-side diff with syntax highlighting, diff colors, and inline changed spans; click it again to fold the file.

Useful flags:

```sh
./rv feature-branch --no-open
./rv main feature-branch --port 9000
```

To run it as `rv` from anywhere, put this repository on your `PATH` or symlink the executable script into a directory already on your `PATH`.
