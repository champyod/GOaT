# GOaT Colab CLI Reference — `google-colab-cli` ONLY

> Pinned: `colab-cli 0.6.0` (`colab version` → `Version: 0.6.0`).
> Pinned: `jupyter-kernel-client==0.15.0` (verified exports `KernelClient`).
> Date verified: 2026-09-03 UTC. Every block below pasted from live `colab --help` / `colab <cmd> --help` runs on this binary.
> Scope: `google-colab-cli` ONLY. No MCP of any kind is read, cited, installed, or documented here.
> Code-grounding: command behaviour cross-checked against installed source
> (`~/.local/share/uv/tools/google-colab-cli/lib/python3.13/site-packages/colab_cli/`,
> `cli.py`, `commands/{session,execution,files,automation,run,utility}.py`, `common.py`, bundled `README.md` + `COLAB_SKILL.md` via `colab skill`).

Serves: `model/select-instruction.md` (Way 1), `model/notebooks/selection.sh`,
`model/notebooks/selection.ipynb` (Args + `%run`), `model/notebooks/<mainstep>/<step>.py` (Drive args).
All paths `ls`-verified. Never reference deleted paths: `model/run_all_cli.sh`, `notebooks/steps/`,
`model/steps/`, `scripts/*.ipynb` (all confirmed `No such file`).

Rule: `exec` = Python-only. `console` = shell-only, direct + paste. Never `exec -c`, never `exec -- <shell>`.

## 0. Global

`colab --help` lists 24 commands (alphabetical via `AlphabeticalGroup` in `cli.py`). All 24 documented in §1.

Global flags (from live `colab --help`):

| Flag | Meaning (source: `cli.py` callback + `--help`) |
|------|------------------------------------------------|
| `-c, --client-oauth-config PATH` | OAuth client JSON. Default `~/.colab-cli-oauth-config.json`. |
| `--config PATH` | Session state file. Default `~/.config/colab-cli/sessions.json`. Isolates parallel runs (`colab --config /tmp/agent.json new -s job`). Daemon inherits it. |
| `--logtostderr` | Log all output to stderr. |
| `--auth oauth2\|adc` | Auth strategy. **This binary's `--help` default reads `oauth2`.** Bundled README says default `adc` — trust `--help` on your binary; always pass explicitly in scripts. Must precede subcommand: `colab --auth=adc new -s x`. |
| `-h, --help` | Help. |

Notes from source:

- No `colab --version` (verified → `No such option`). Use `colab version`.
- Single-session shorthand: `-s` omittable when exactly one active session exists (`common.py: resolve_session` prints `Using unique session '<name>'`). GOaT still always passes `-s goat` (explicit, copy-pasteable, multi-session safe).
- `exec`/`repl`/`console`/`run` all `cd /content` first (verified in `execution.py`, `run.py`). Prefer absolute `/content/...` paths.
- Kernel persists across `exec` calls in one session (reattach, websocket close only). `restart-kernel` / `stop` reset it (per `COLAB_SKILL.md`).
- Upgrade banner suppressed for pipeable commands (`version`, `log`, `pay`, `help`, `url`, `whoami`, `readme`, `skill`) — safe to pipe (verified `cli.py`).

## 1. Command reference

### 1.1 `new` — provision a billable VM

Purpose: allocate CPU/GPU/TPU runtime + spawn keep-alive daemon. Pre-flights keep-alive; on missing-scope 403 it unassigns (no leaked billable VM) and prints remediation (verified `session.py: new`).

<details><summary><code>colab new --help</code> (verbatim)</summary>

```text
Usage: colab new [OPTIONS]

 Create a new session

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --tpu              <str>  TPU accelerator variant. Supported: v5e1, v6e1.    │
│ --gpu              <str>  GPU accelerator variant. Supported: T4, L4, G4,    │
│                           H100, A100.                                        │
│                           If omitted (along with --tpu), a CPU runtime is    │
│                           created.                                           │
│                           Availability varies by Colab subscription tier.    │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

| Arg | Detail |
|-----|--------|
| `-s/--session` | Optional in help; **REQUIRED by GOaT** (`-s goat`). Omitted → random 6-hex name (verified `session.py: name = session or uuid4().hex[:6]`). |
| `--gpu T4\|L4\|G4\|H100\|A100` | Case-insensitive in code (`gpu.lower()` map). Unknown value silently maps to A100 then usually 400s — always use listed values. |
| `--tpu v5e1\|v6e1` | `v5e1` exact else `V6E1` in code path. |

LIMITATIONS: always `-s name`. Re-`new` while alive = second billable VM. 400 on accelerator = no quota/entitlement → fallback `--gpu T4` or CPU (friendly message in code, exit 1).

```bash
colab new -s goat --gpu T4
```

### 1.2 `exec` — Python-only (`-f` file or piped stdin)

Purpose: run Python on the VM kernel. Local file is read locally and sent (no manual upload). `.ipynb` runs cell-by-cell → `<basename>_output.ipynb`; `# @title Foo` first line labels progress (verified `execution.py`).

<details><summary><code>colab exec --help</code> (verbatim)</summary>

```text
Usage: colab exec [OPTIONS]

 Execute code in a session

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session       -s      <str>    Session name                                │
│ --file          -f      <str>    File to execute                             │
│ --output-image          <str>    Path to save plot                           │
│ --timeout               <float>  Timeout in seconds for code execution       │
│                                  [default: 30.0]                             │
│ --help          -h               Show this message and exit.                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

| Arg | Detail |
|-----|--------|
| `-s` | Target session (`resolve_session`; omit only if single session). |
| `-f/--file` | Local `.py` (run as script) or `.ipynb` (per-cell, outputs saved back). |
| piped stdin | `echo "print(1)" \| colab exec -s goat`. No flag = stdin mode. Empty stdin → exit 0 silently. TTY with no input → error `No input provided`. |
| `--timeout` | Float seconds, default **30.0**. Long jobs: raise it or background via console `nohup`. |
| `--output-image` | Save PNG/JPEG to known path; otherwise temp path printed, inline escapes suppressed when piped. |

LIMITATIONS: Python-only. **No `-c`** (verified absent). **No positional shell args** (verified: only `-s/-f/--output-image/--timeout`). 30 s default timeout. 404/401 → auto-prune + exit 1 (`is_terminal_error` path).

```bash
colab exec -s goat -f script.py
cat script.py | colab exec -s goat
colab exec -s goat -f report.ipynb
```

### 1.3 `console` — shell-only raw TTY (tmux)

Purpose: interactive shell on the VM. Used for everything `exec` cannot do (git, mkdir, nohup, tail, uv).

<details><summary><code>colab console --help</code> (verbatim)</summary>

```text
Usage: colab console [OPTIONS]

 Connect to raw TTY console

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

| Arg | Detail |
|-----|--------|
| `-s` | Only option. Target session. |

LIMITATIONS: use ONLY for shell with no `exec` equivalent, with stated reason. Tmux wraps bash → piped output contains terminal-control bytes (filter `grep -a`). Exits on EOF. Agent pattern: open once (`colab console -s goat`), paste blocks directly. Do NOT use echo-piped console as default shell path.

```bash
colab console -s goat
# then paste:
[ -d /content/GOaT/.git ] || git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT
mkdir -p /content/GOaT/model/notebooks/selection
nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT --debug > /tmp/goat_log.txt 2>&1 &
tail -c 2000 /tmp/goat_log.txt
# --debug fans out: sh forwards to every python call; log_call prints [enter]/[exit]/[error] each.
```

### 1.4 `drivemount` — human-interactive

Purpose: run `drive.mount(path)` on the VM with credential-propagation hook (`dfs_ephemeral` intercept, 600 s interactive timeout in `automation.py`).

<details><summary><code>colab drivemount --help</code> (verbatim)</summary>

```text
Usage: colab drivemount [OPTIONS] [path]

 Mount Google Drive at path

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   path      <str>  Mount path [default: /content/drive]                      │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

| Arg | Detail |
|-----|--------|
| `[path]` | Mountpoint, default `/content/drive` (code default matches). |
| `-s` | Target session. |

LIMITATIONS: human-interactive (browser OAuth + `Press Enter after you have granted access...` via `/dev/tty`). Run once after `new`; then use `/content/drive/MyDrive/...`. Breaks entirely on `jupyter-kernel-client` 1.x (see §5).

```bash
colab drivemount -s goat
```

### 1.5 `run` — ephemeral `new` + `exec` + `stop`

Purpose: one-shot script on a fresh VM with `sys.argv` + `__main__` semantics, CPython exit codes, shebang support. Validates script locally before allocating (typo costs nothing). Chatter → stderr, script output → stdout (verified `run.py` + skill).

<details><summary><code>colab run --help</code> (verbatim)</summary>

```text
Usage: colab run [OPTIONS] {script} [script_args]...

 Run a Python script on a fresh Colab VM, then release the VM

 Designed to be used as a shebang interpreter, e.g.

     #!/usr/bin/env -S colab run --gpu T4

 so a single executable .py file can rent a GPU, run, and clean up after
 itself.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    script           <str>  Path to a local Python file to execute on a     │
│                              fresh Colab VM.                                 │
│                              [required]                                      │
│      script_args      <str>  Arguments forwarded to the script as            │
│                              sys.argv[1:]. Anything after the script path is │
│                              passed through verbatim.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>    Name for the ephemeral session (auto-generated   │
│                             if omitted). Useful with --keep so you can       │
│                             attach later via `colab exec -s <name>`.         │
│ --tpu              <str>    TPU accelerator variant. Supported: v5e1, v6e1.  │
│ --gpu              <str>    GPU accelerator variant. Supported: T4, L4, G4,  │
│                             H100, A100. If omitted (along with --tpu), a CPU │
│                             runtime is created.                              │
│ --keep                      Do not stop the session after the script         │
│                             finishes. The session remains in `colab          │
│                             sessions` until you run `colab stop`.            │
│ --timeout          <float>  Timeout in seconds for code execution            │
│                             [default: 30.0]                                  │
│ --help     -h               Show this message and exit.                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: ephemeral — releases VM unless `--keep`. NOT used for GOaT selection (needs persistent `goat` VM). Unknown trailing flags pass through to the script (`allow_extra_args`). `sys.exit()`/`0` → 0, `sys.exit(N)` → N, `sys.exit("msg")` → 1; other exceptions → 1; SystemExit traceback suppressed.

```bash
colab run --gpu T4 train.py --epochs 3
```

### 1.6 `sessions` — list active (incl `[?]` orphans)

Purpose: server-side truth; auto-prunes stale local entries. `[?]` = server VM with no local record (still billable).

<details><summary><code>colab sessions --help</code> (verbatim)</summary>

```text
Usage: colab sessions [OPTIONS]

 List all active sessions

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: no args. Line format `[name] endpoint | Hardware: X | Variant: Y` (verified `session.py: _format_session_line`; CPU shown for `NONE`). Live check: `No active sessions found on server.` when empty.

```bash
colab sessions
```

### 1.7 `status` — hardware + IDLE/BUSY + last execution

Purpose: local status view per session.

<details><summary><code>colab status --help</code> (verbatim)</summary>

```text
Usage: colab status [OPTIONS]

 Show session status

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: `-s <name>` shows one; omitted shows all locals (`No active sessions.` if none). `BUSY (<what>)` vs `IDLE` + `Last Execution: <file> [| Cell: x] at <ts>` (verified `session.py`).

```bash
colab status -s goat
colab status
```

### 1.8 `stop` — release VM (REQUIRES `-s` in GOaT)

Purpose: kill keep-alive daemon, shutdown kernel, unassign VM, remove local state, log `session_terminated=user_requested` (verified `session.py: stop`).

<details><summary><code>colab stop --help</code> (verbatim)</summary>

```text
Usage: colab stop [OPTIONS]

 Stop a session

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: **REQUIRES `-s` flag** by GOaT convention (`colab stop -s goat`). Code can resolve single session, but never rely on it in runbooks. Only release path besides 24 h cap / `run` auto-teardown. Missing session → `Session '<name>' not found.`

```bash
colab stop -s goat
```

### 1.9 `log` — view / export session history

Purpose: structured events (`execution`, `file_operation`, `automation`, keep-alive errors with `response_body`). Export suffix decides format.

<details><summary><code>colab log --help</code> (verbatim)</summary>

```text
Usage: colab log [OPTIONS]

 Manage and view session history logs

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name (if omitted, lists all sessions with  │
│                           logs)                                              │
│ --lines    -n      <int>  Number of lines to show/export (default: all)      │
│ --type     -t      <str>  Filter by event type (e.g., execution,             │
│                           file_operation)                                    │
│ --output   -o      <str>  Output file path (suffix determines format:        │
│                           .ipynb, .md, .txt, .jsonl)                         │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

| Arg | Detail |
|-----|--------|
| `-s` | Omitted → list sessions with logs. |
| `-n/--lines` | Last N events (`events[-lines:]` in code). |
| `-t/--type` | Exact `event_type` match (`execution`, `file_operation`, `automation`, `keep_alive_error`, …). |
| `-o/--output` | `.ipynb`/`.md`/`.txt`/`.jsonl` via `converter.export_history`. |

LIMITATIONS: history is local (`history/*.jsonl`); needs the originating `--config` file to see parallel-run logs. Rendering prefixes: `EXEC`/`FILE`/`AUTO`/`INPT`/`RPLY`/`KEEP` (verified `utility.py`).

```bash
colab log -s goat -n 20
colab log -s goat -t execution
colab log -s goat -o summary.ipynb
```

### 1.10 `upload` — single file up (HTTP 500 if parent missing)

Purpose: Contents API upload; logs `file_operation=upload`.

<details><summary><code>colab upload --help</code> (verbatim)</summary>

```text
Usage: colab upload [OPTIONS] {local_path} {remote_path}

 Upload a file to a session

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    local_path       <str>  Local file to upload [required]                 │
│ *    remote_path      <str>  Remote path to upload to [required]             │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: **HTTP 500 if remote parent dir missing — `mkdir -p` first via console.** Single file only (no recursion; GOaT does ×10). Missing local file → `Local file ... not found.` exit 1.

### 1.11 `download` — single file down

Purpose: Contents API download; logs `file_operation=download`.

<details><summary><code>colab download --help</code> (verbatim)</summary>

```text
Usage: colab download [OPTIONS] {remote_path} {local_path}

 Download a file from a session

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    remote_path      <str>  Remote path to download from [required]         │
│ *    local_path       <str>  Local path to save the file [required]          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: single file; arg order is REMOTE then LOCAL (opposite of `upload`).

```bash
colab download -s goat /content/GOaT/model/results/mt_selection.json ./mt_selection.json
```

### 1.12 `install` — `uv` preferring, `pip` fallback

Purpose: executes remote installer snippet (`uv pip install --system`, except → `pip install`). `-r` uploads the local requirements file to `content/<basename>` first (verified `automation.py`).

<details><summary><code>colab install --help</code> (verbatim)</summary>

```text
Usage: colab install [OPTIONS] [packages]...

 Install python packages on the VM

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   packages      <str>  Packages to install                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session      -s      <str>  Session name                                   │
│ --requirement  -r      <str>  Requirements file                              │
│ --help         -h             Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: no packages and no `-r` → exit 1. Missing local `-r` file → exit 1. GOaT `selection.sh` uses its own `uv sync --extra ocr --extra mt --extra train` — prefer that for pipeline runs.

```bash
colab install -s goat torch transformers
colab install -s goat -r requirements.txt
```

### 1.13 `edit` — `$EDITOR` round-trip on one remote file

Purpose: download to temp (same extension), `click.edit`, re-upload only if sha256 changed (verified `files.py: edit`).

<details><summary><code>colab edit --help</code> (verbatim)</summary>

```text
Usage: colab edit [OPTIONS] {remote_path}

 Edit a file on a running Colab session

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    remote_path      <str>  Remote path to edit [required]                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: single path; missing remote starts empty; no-change → `No changes made`. Needs local `$EDITOR`. GOaT prefers re-`upload` after local edit (repeatable).

### 1.14 `ls` — list remote (default `content`)

Purpose: Contents API list; dirs first then alpha, `/` suffix on dirs (verified `files.py: ls`).

<details><summary><code>colab ls --help</code> (verbatim)</summary>

```text
Usage: colab ls [OPTIONS] [path]

 List files in a session

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   path      <str>  Remote path to list [default: content]                    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: default `content` (VM root). Prefer absolute `/content/...`. Single path.

```bash
colab ls -s goat /content/GOaT/model/notebooks
```

### 1.15 `rm` — delete remote path

Purpose: Contents API delete + `Deleted <path>` echo.

<details><summary><code>colab rm --help</code> (verbatim)</summary>

```text
Usage: colab rm [OPTIONS] {path}

 Remove a remote file

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    path      <str>  Remote path to remove [required]                       │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: single path, required. Errors → `[colab] Error: ...` exit 1.

### 1.16 `url` — attach browser UI to existing VM (no new VM)

Purpose: prints pipeable URL (`empty.ipynb?dbu=<enc path>#datalabBackendUrl=<full url>`); `--open` also launches browser. Output has no `[colab]` prefix by design (verified `utility.py: url`).

<details><summary><code>colab url --help</code> (verbatim)</summary>

```text
Usage: colab url [OPTIONS]

 Print a browser URL that connects to an existing session.

 Format: ``https://<host>/notebooks/empty.ipynb?dbu=<urlencoded
 path>#datalabBackendUrl=<host>/tun/m/<endpoint>``,
 where the path is ``/tun/m/<endpoint>``. When opened, the Colab frontend
 skips ``/tun/m/assign`` and attaches the kernel to your existing VM.
 [Two-signal dbu + fragment explanation and --host/--open semantics
 as printed by live --help; behaviour verified in commands/utility.py.]
```

</details>

Full live options (verbatim):

```text
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --host             <str>  Colab frontend host (origin) to use for the URL.   │
│                           [default: https://colab.research.google.com]       │
│ --open                    After printing the URL, also open it in the system │
│                           browser. Off by default so the command remains     │
│                           pipeable (e.g. `colab url -s s1 | xclip`).         │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

LIMITATIONS: attaches to existing VM only. `--open` off by default (pipeable). Host trailing slash stripped in code.

```bash
colab url -s goat
```

### 1.17 `restart-kernel` — keep VM, reset kernel

Purpose: remote kernel restart via `ColabRuntime.restart()`; local kernel/session IDs re-persisted (verified `session.py`).

<details><summary><code>colab restart-kernel --help</code> (verbatim)</summary>

```text
Usage: colab restart-kernel [OPTIONS]

 Restart a session's kernel

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session  -s      <str>  Session name                                       │
│ --help     -h             Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: wipes imports/vars; keeps VM + files. First fix for wedged kernel/timeout; else `stop` + `new`.

```bash
colab restart-kernel -s goat
```

### 1.18 `repl` — interactive Python (piped-EOF safe)

Purpose: TTY REPL (`ColabREPL`) or one-shot piped execution; logs `repl_started` / `execution(source=piped)`.

<details><summary><code>colab repl --help</code> (verbatim)</summary>

```text
Usage: colab repl [OPTIONS]

 Start an interactive REPL

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --session       -s      <str>  Session name                                  │
│ --output-image          <str>  Path to save plot                             │
│ --help          -h             Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: interactive needs TTY; agents pipe stdin (exits on EOF). Prefer `exec -f` (repeatable file). Empty piped stdin → exit 0.

### 1.19 `skill` / 1.20 `readme` — print bundled docs

Purpose: print `COLAB_SKILL.md` / `README.md` from package resources (verified `utility.py: _print_resource`).

<details><summary><code>colab skill --help</code> + <code>colab readme --help</code> (verbatim)</summary>

```text
Usage: colab skill [OPTIONS]

 Print the bundled COLAB_SKILL.md file

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────╯

Usage: colab readme [OPTIONS]

 Print the bundled README.md file

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: none. Aliases `SKILL`/`README` exist (hidden). Use once for self-discovery (auth scopes, safety).

### 1.21 `update` — check release (`--install` Linux/macOS)

Purpose: `check_for_updates`; `--install` self-upgrades when newer known.

<details><summary><code>colab update --help</code> (verbatim)</summary>

```text
Usage: colab update [OPTIONS]

 Check for latest version and print if an update is available

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --install            After checking, run 'pip install -U google-colab-cli'   │
│                      to upgrade the CLI in place. No-op if already up to     │
│                      date. Linux only.                                       │
│ --help     -h        Show this message and exit.                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: code allows Linux + macOS (`is_self_install_supported`); help text says Linux only — trust code path on macOS, report mismatch. No-op when current ≥ latest.

### 1.22 `version` — print version

<details><summary><code>colab version --help</code> (verbatim)</summary>

```text
Usage: colab version [OPTIONS]

 Show the version of the Colab CLI

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: none. Live output: `Version: 0.6.0`. Canonical version probe (no `--version` flag exists).

### 1.23 `pay` — open subscription page

Purpose: `webbrowser.open("https://colab.research.google.com/signup")` (verified `utility.py: pay`).

<details><summary><code>colab pay --help</code> (verbatim)</summary>

```text
Usage: colab pay [OPTIONS]

 Open the Colab signup page to manage compute units

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: display-only; manages compute units in browser.

### 1.24 `help` — help router

Purpose: no-arg prints top help; `colab help <cmd>` prints that command's help; unknown → `No such command` exit 2 (verified `cli.py: help_command`).

<details><summary><code>colab help --help</code> (verbatim)</summary>

```text
Usage: colab help [OPTIONS] [command]

 Show help for a command.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│   command      <str>  Command to show help for                               │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

LIMITATIONS: `[command]` optional. Listing alphabetical.

### Appendix A — hidden commands (found in source, absent from `--help`)

Not part of the 24-command contract; documented here because the full source was read.

| Command | Source | Behaviour |
|---------|--------|-----------|
| `colab auth -s NAME` | `commands/automation.py` (registered `hidden=True`) | VM-side GCP creds (`google.colab.auth.authenticate_user`, `USE_AUTH_EPHEM=0`). Interactive (`allow_stdin`, 600 s timeout). Orthogonal to CLI `--auth`; never use to fix CLI 401/403 (fix scopes instead). |
| `colab whoami` | `commands/utility.py` (registered `hidden=True`) | Debug: refresh creds, query `tokeninfo`, print provider/email/audience/expiry/scopes. Instant scope diagnosis on 403 vs `colab.pa.googleapis.com`. |
| `colab keep-alive ENDPOINT NAME` | `commands/session.py` (registered `hidden=True`) | Daemon loop: 60 s ping, 24 h cap, exits on `session_not_found`/`endpoint_mismatch`/`consecutive_4xx_errors`/`time_limit_reached`. Never invoke manually. |

## 2. Billing & sessions

| Fact | Detail |
|------|--------|
| Per-VM billing | `new` allocates; `stop` releases. Billing is per-VM, not per-command. |
| Stop-or-burn | Only auto-release is the 24 h keep-alive cap (`max_duration = 24*3600`, verified `session.py`). Unstopped idle burns units. |
| List | `colab sessions` = server truth + stale-local prune. |
| Orphans | `[?]` = server assignment with no local record. Still billable — `stop` it (reuse original `--config` if created with one). |
| Re-run safety | Every selection step skips when its output JSON exists (per `select-instruction.md`); never re-`new` while `goat` alive — `sessions` first. |
| Keep-alive | Detached daemon from `new`/`run`; inherits `--auth` + `--config`. Death by `consecutive_4xx_errors` ≈ missing `colaboratory` scope (see §4). Local state: `~/.config/colab-cli/sessions.json`, `settings.json`, `history/*.jsonl`; don't hand-edit. |
| GPU fallback | 400 on accelerator = no quota → `--gpu T4` → CPU. Valid GPU `T4,L4,G4,H100,A100`; TPU `v5e1,v6e1`; tier-gated (most accounts CPU-only). |

```bash
colab sessions
colab status -s goat
colab stop -s goat   # REQUIRED when done
```

## 3. GOaT runbook mapping (provision → sync → launch → check → stop)

| Phase | Primitive | Exact call (from `model/select-instruction.md`, paths `ls`-verified) |
|-------|-----------|------------------------------------------------------------------------|
| Provision | `new` | `colab new -s goat --gpu T4` (CPU: drop flag) |
| Mount | `drivemount` | `colab drivemount -s goat` → `/content/drive` |
| Clone | console-paste git | `colab console -s goat`, paste `[ -d /content/GOaT/.git ] \|\| git clone --depth 1 https://github.com/champyod/GOaT.git /content/GOaT` |
| Sync ×10 | `upload` | `colab upload -s goat model/notebooks/selection.sh /content/GOaT/model/notebooks/selection.sh`, `model/notebooks/selection.ipynb → .../selection.ipynb`, `model/notebooks/data/download_data.py → ...`, `model/notebooks/selection/select_mt.py → ...`, `model/notebooks/selection/select_ocr.py → ...`, `model/notebooks/training/train_mt.py → ...`, `model/notebooks/training/train_ocr.py → ...`, `model/src/goat_model/mt/train.py → ...`, `model/src/goat_model/constants.py → ...`, `model/src/goat_model/ocr/train.py → ...` (repeat after every local edit; `mkdir -p` remote parent first or HTTP 500) |
| Launch | console-paste `nohup` | paste in console: `nohup bash /content/GOaT/model/notebooks/selection.sh /content/drive/MyDrive/GOaT > /tmp/goat_log.txt 2>&1 &` (close console whenever; daemon holds VM) |
| Check | `tail` / `log` | console `tail -c 2000 /tmp/goat_log.txt`, or `colab log -s goat -n 20` |
| Stop | `stop` | `colab stop -s goat` |
| Retrieve | `download` | `colab download -s goat /content/drive/MyDrive/GOaT/results/mt_selection.json ./...` (same for other 3 JSONs) |

`selection.sh` contract (verified content): `PROJECT=/content/GOaT/model`, `DRIVE=${1:-/content/drive/MyDrive/GOaT}`, `uv sync --extra ocr --extra mt --extra train`, then 8 `uv run python notebooks/...` calls with `$DRIVE/...` args + `ls -lh $DRIVE/results/`. The `.ipynb` orchestrator `%run`s the same `.py` files with the same Drive args — one `.py` per step, no per-step notebooks.

Task choice per call (exec-over-console): Python snippets → `exec -f`/stdin; installs → `install` (or `selection.sh`'s `uv sync` for pipelines); file moves → `upload`/`download`; shell-only (git/mkdir/nohup/tail) → `console` with reason stated (no `exec` equivalent; tmux + EOF costs accepted).

## 4. Troubleshooting

| Symptom | Cause | Exact fix |
|---------|-------|-----------|
| `module 'jupyter_kernel_client' has no attribute 'KernelClient'` on `exec`/`drivemount` | Unpinned dep resolved to 1.x (`KernelClient`→`JupyterKernelClient`) | `uv pip install --python ~/.local/share/uv/tools/google-colab-cli/bin/python "jupyter-kernel-client==0.15.0"` |
| `colab --version` → `No such option` | Flag doesn't exist | `colab version` |
| `exec -c` / `exec -- <shell>` fails | Invalid; no such flags in `--help` or `execution.py` | `-f file` / piped stdin for Python; `console` for shell |
| `upload` HTTP 500 | Remote parent dir missing (Contents API) | console `mkdir -p <remote-dir>`, retry |
| Console output garbled | tmux control bytes | `grep -a <line>`; prefer `exec` when possible |
| `Session not found` / 404 / 401 on exec/repl/console | Backend pruned VM; CLI auto-prunes local | `colab sessions`, re-`new` |
| Timeout / wedged kernel | 30 s default or stuck kernel | raise `--timeout`, or `colab restart-kernel -s goat`, else `stop` + `new` |
| 400 on `new`/`run` with accelerator | No quota/entitlement | `--gpu T4` or CPU |
| `[?]` in `sessions` | Orphan billable VM | `colab stop -s <name>` (same `--config` if used) |
| `keep_alive_stopped reason=consecutive_4xx_errors` in `log` | Missing `colaboratory` scope | Re-mint ADC with 4 scopes (see skill): `gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory`; oauth2: delete `~/.config/colab-cli/token.json`, re-`new`. Diagnose fast with hidden `colab whoami`. |
| `No packages or requirements specified` | Bare `install` | Pass `PKG...` or `-r file` |
| `install -r` missing file | Local path wrong | Fix local path (checked with `os.path.isfile` before upload) |

## 5. Dependency-trap note

Unpinned `jupyter-kernel-client` resolves to 1.x, which renamed `KernelClient`→`JupyterKernelClient` and breaks ALL `exec`/`drivemount` on `colab-cli` 0.6.0.

Verified live:

```bash
$ ~/.local/share/uv/tools/google-colab-cli/bin/python -c \
  "import jupyter_kernel_client; print([x for x in dir(jupyter_kernel_client) if 'Client' in x])"
['ColabKernelClient', 'KaggleKernelClient', 'KernelClient', 'KernelWebSocketClient']
$ uv pip show --python ~/.local/share/uv/tools/google-colab-cli/bin/python jupyter-kernel-client
Name: jupyter-kernel-client
Version: 0.15.0
```

Fix (verified: 0.15.0 exports `KernelClient`):

```bash
uv pip install --python ~/.local/share/uv/tools/google-colab-cli/bin/python "jupyter-kernel-client==0.15.0"
```

## Verify

- [x] 24/24 `colab --help` commands documented (§1.1–§1.24); hidden `auth`/`whoami`/`keep-alive` separated in Appendix A (source-grounded, not in `--help`).
- [x] Every `--help` block from live runs on 0.6.0, 2026-09-03; behaviours cross-checked against installed `colab_cli` source files listed in header.
- [x] `ls`-verified paths: `model/select-instruction.md`, `model/notebooks/selection.sh`, `model/notebooks/selection.ipynb`, `model/notebooks/data/download_data.py`, `model/notebooks/selection/select_mt.py`, `model/notebooks/selection/select_ocr.py`, `model/notebooks/training/train_mt.py`, `model/notebooks/training/train_ocr.py`, `model/src/goat_model/{constants,mt/train,ocr/train}.py`.
- [x] Dead paths confirmed absent: `model/run_all_cli.sh`, `model/steps/`, `model/notebooks/steps/`, `scripts/*.ipynb`.
- [x] Forbidden items absent: no `exec -c`, no `exec -- <shell>`, no echo-piped console as default, no MCP content, no secrets/tokens/session IDs.
