# AI Dev Director · HanginIs edition

[한국어](README.md) · [English](README.en.md)

A local development director that runs three logged-in AI coding CLIs (Claude Code, OpenAI Codex, Google Antigravity) as HEAD, BUILDER and REVIEWER, and turns a one-line goal into code that has been tested and independently reviewed. It never uses metered APIs.

This repository is a fork of [ai-dev-director by DAVZD](https://github.com/digilogd-cmd/ai-dev-director) v3.1.8, changed to work on macOS. The original is Windows-first. This fork adds a native macOS app and hardens how results are applied, how missions are judged and how repositories are handled.

- **Status:** used daily on macOS. All 649 tests pass on macOS (2026-11-11; one test that pretends to be Windows is skipped).
- **Not verified:** the changes in this fork have not been run on Windows or Linux. TRIAD research mode is the original feature unchanged; beyond passing unit tests it has not been verified in this fork.

The console and all mission messages are in Korean.

## What it does

```
goal ─▶ HEAD (writes an ORDER) ─▶ BUILDER (proposes file changes as JSON)
              ▲                              │
              │                              ▼
       HEAD decision ◀── REVIEWER ◀── verification (build and tests in an isolated worktree)
```

- **Roles:** by default HEAD is Claude Code, BUILDER is Antigravity and REVIEWER is Codex. When one CLI hits its quota or fails, another takes over. Two CLIs are enough.
- **Missions:** one chat line becomes a mission. HEAD plans one milestone at a time and plans the next one from the test and review results of the last.
- **Isolation:** every change is made in a git worktree under `.director_worktrees/` inside the project. The project itself changes only in the apply step after the mission ends.
- **Apply:** a result that passed is applied to the project automatically, depending on settings. The tests are run again in the project after applying, and the change is rolled back if they fail. Nothing is committed.

## Requirements

- macOS 13 or later
- Python 3.9 or later, Git
- Xcode (Swift 5.9 or later) to build the app
- At least two logged-in CLIs: `claude`, `codex`, `agy`. Their subscription login sessions are used as they are. No API key is needed, and any key in the environment is removed before a CLI is called.

## Install and run

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./script/build_and_run.sh              # builds dist/AI-Dev-Director.app and opens it
./script/build_and_run.sh --build-only # build only
```

The app starts the server and shows the console in its window. The build records the absolute path of the repository, so rebuild after moving the repository. The app itself can be copied to the Applications folder.

- **Choose a project:** 보기 (View) → 프로젝트 폴더 선택 (⌘O). The standard macOS folder dialog opens.
- **Quit:** on quit you choose between "백그라운드 계속 실행" (close the window, keep the server and the work running) and "작업 중지 후 종료" (stop the work, then quit).
- **Lost connection:** 보기 (View) → 콘솔 다시 연결 (⌘R).

Without the app, start the server and open it in a browser.

```bash
./start-companion.sh    # http://127.0.0.1:8081 (stage view), /classic (workshop view)
./stop-companion.sh
```

A mission can also be run from the terminal.

```bash
./.venv/bin/python director.py do "fix the login bug" -p /path/to/project --profile quick
./.venv/bin/python director.py status
./.venv/bin/python director.py clean-worktrees -p /path/to/project --dry-run
```

`do` starts the mission through the server when one is running, so it shows in the console, and otherwise runs it in the terminal. With `--no-apply` the result stays in the worktree and is not applied to the project.

## Reading a mission result

When a mission ends, one message gives the outcome and whether it was applied.

| Status | Meaning |
| --- | --- |
| 미션 완료 (DONE) | HEAD judged the goal met, and at least one milestone passed. |
| 미션 중단 (INCOMPLETE) | The milestone budget ran out, or HEAD said done but no milestone passed. |
| 미션 실패 (FAILED) | The consecutive-failure limit was reached, or the verification command itself failed twice in a row for the same reason. |
| 미션 보류 (BLOCKED) | Something a person must decide came up: payment, live-account orders, real credentials, or an irreversible external action. |
| 정지 (STOPPED) | You stopped it. It does not resume by itself after a restart. |

The last line reports the apply step.

- **"결과물을 프로젝트에 적용했습니다"** (applied): the files were written to the project and the tests passed again there.
- **"적용할 변경이 없습니다"** (nothing to apply): the mission only checked and found nothing to fix, or the project already has the same content. This is not an error.
- **"자동 적용하지 않았습니다 (reason)"** (not applied automatically): applying was held back because of a conflict, verification that did not run, a worktree changed after review, and so on. The worktree is kept, and you can apply or discard it from the console.

## Rules it enforces

| Rule | Behavior |
| --- | --- |
| No metered APIs | Every `*_API_KEY` is removed from the environment before a CLI is called. |
| Project isolation | Building and editing happen only in worktrees under `.director_worktrees/`. |
| Scope limits | The Director checks the allowed paths, protected paths, file count and changed-line count written in the ORDER. The BUILDER cannot relax them. |
| A failed review is a failure | A missing or broken review reply counts as a failure, not a pass. |
| Only what was reviewed is applied | The file contents at the moment of the pass are recorded and compared with the worktree right before applying. A worktree changed after review is not applied. |
| Your edits win | If the AI and you changed the same file, apply is blocked as a conflict. A file you deleted is not brought back, and deletions are never applied. |
| Unverified results need a person | A result for which no build or test ran is never applied automatically. A person can apply it from the console. |
| Money and credentials need a person | The mission stops at payments, live-account order code, account creation and irreversible external actions. |
| Stop means stop | "중단", "멈춰" or "stop" in chat, or the stop button in the console, ends the CLIs that mission started at once. Other missions keep running. |
| No repository inside a repository | If the chosen project folder is already inside another git repository, no new repository is created; you are told to choose the repository root. |

## Verification command

The command used to verify each project is chosen in this order.

1. A command registered under `projects` in `config.yaml` or `config.local.yaml`
2. pytest, if a `tests/` folder has Python test files (when the project has no pytest settings, a `pytest.ini` in a parent folder is not used)
3. `swift test`, if `Package.swift` has a test target
4. `npm test`, if `package.json` has a test script
5. `Tests/run.sh`, `tests/run.sh` or `test/run.sh`, if one exists
6. `make test`, if the `Makefile` has a `test` target

Folder names are matched with exact case. The default macOS disk ignores case, but a Swift `Tests/` folder is not treated as a Python `tests/` folder.

When nothing is found, nothing is run and the result is recorded as **not run (NOT_RUN)**. Such a result is not applied automatically. If the verification command fails on two milestones in a row with "no tests" (exit code 4 or 5) or "command not found" (126 or 127), the BUILDER cannot fix it, so the mission ends and asks you to register a command.

Verification commands live in the Director's settings and not inside the project, so that the BUILDER cannot weaken them.

## Configuration

- **`config.yaml`:** CLI per role, timeouts, safety caps (`max_files_changed`, `max_diff_lines`), default verification commands, autonomy mode and profiles.
- **`config.local.yaml`:** settings that fit one computer only. It is not tracked by git and is merged over `config.yaml`.
- **`companion.json`:** console port (8081 by default) and the list of allowed projects.

```yaml
# config.local.yaml example
verification:
  timeout: 300        # seconds for one verification command; a first `swift test` has to compile
projects:
  "/path/to/my-app":
    verification:
      test_command: ["sh", "-c", "swift run MyAppChecks"]
```

| Profile | Milestones | Automatic repairs | Builder attempts |
| --- | --- | --- | --- |
| `quick` | 2 | 1 | 2 |
| `standard` (default) | 6 | 3 | 3 |
| `deep` | 12 | 3 | 3 |

`autonomy.auto_apply` is one of `off`, `on_done` (only when the goal was judged met) and `on_pass` (whenever there is a result that passed; the default).

The console listens on `127.0.0.1` only. Requests that change state require a local-origin check and a token (`data/solomon-bridge.token`, readable only by you).

## Where a follow-up mission starts

A new mission starts from the current state of the project by default. It continues from the previous mission's worktree only when all three conditions hold: that worktree passed review, it has not been applied to the project yet, and the project has no new commit since that mission started. If the project changed in the meantime, the mission starts from the project again, so that work on an old worktree cannot undo newer changes.

## Data and logs

| Location | Contents |
| --- | --- |
| `data/director.db` | Runs, states and usage (SQLite, WAL mode) |
| `data/missions/` | Plan and milestone results per mission |
| `runs/<run id>/` | ORDER, verification result, review, decision, review record and apply backup per milestone |
| `<project>/.director_worktrees/` | Worktrees per milestone. Clean them with `director.py clean-worktrees`. |
| `data/desktop-server.log` | Log of the server started by the app. Above 5 MB it is moved to `.1` when the app starts, and a new file begins. Successful read requests are not logged. |

When the console window is hidden or minimized it stops polling for status, and it catches up as soon as it is visible again. Runs left "in progress" because the app or server was shut down are closed the next time the server starts.

## Layout

```
director.py            CLI entry point (run / mission / do / status / resume / clean-worktrees)
companion_server.py    console server (8081)
dashboard_server.py    workshop view, chat, mission stop, status summary
agents/                HEAD / BUILDER / REVIEWER adapters
core/                  missions, verification, applying results, scope checks, process control, provider failover, TRIAD research
schemas/               formats for ORDER, builder result, review and decision
console/               stage and workshop views (static files)
macos/                 macOS app source (Swift package)
script/                app build script
tests/                 tests
docs/                  design records
```

## Tests

```bash
./.venv/bin/python -m pytest -q     # about 5 minutes; runs on stubs, without the real CLIs
```

Unit tests do not run the real CLIs and do not touch the working database. Integration tests that use the real CLIs run only with `-m integration_cli`.

## What changed from the original

- **macOS app:** a native Swift, AppKit and WKWebView app, the standard folder dialog, quit handling and an app icon.
- **Verification command detection:** runs the tests the project actually supports. The original ran `python -m pytest tests/` on every project, so Swift and JS projects failed at every milestone.
- **"Not run" as its own state:** pass, fail and not-run are told apart, and a result that was not verified is not applied automatically.
- **Apply protection:** only reviewed files are applied, a file you deleted is a conflict, and the state of a result that was already applied is not overwritten.
- **Mission verdicts:** a mission with no passed milestone is not shown as done, repeated verification failures that cannot be fixed end the mission, a "nothing has to change" answer is accepted, and follow-up missions prefer the current project state.
- **Stop and recovery:** only the processes of the named mission are ended, and leftover runs are closed when the server starts.
- **Repositories and database:** no repository is created inside another one; SQLite runs in WAL mode with a wait time.
- **macOS paths:** executable names and document paths are read correctly in macOS form as well.
- **Console:** the Solomon link and the broadcast mode were removed, and the workshop view has a link back to the stage view.

## Known limits

- Even large changes are submitted by the BUILDER as file contents in JSON, so they can hit a CLI's output limit.
- It is bound by each CLI's subscription quota. When a quota runs out, another CLI takes over.
- When the reviewer and HEAD end up on the same CLI, the run continues with a `REDUCED_INDEPENDENCE` mark.
- The changed-line cap applies per milestone only. There is no cap on the total change of a mission that spans several milestones.
- HEAD and the reviewer do not read the project's past decisions (its change history) on their own. A plan that reverses an existing safety rule can pass, so read the changes yourself before applying a result.
- Runs that finished before these changes have no review record, so applying them is refused. They have to be reviewed again.

## Credits and license

The original is [ai-dev-director](https://github.com/digilogd-cmd/ai-dev-director) v3.1.8 by DAVZD. The changes in this fork were made by HanginIs. What to fix was decided by combining separate code reviews from three AIs: Codex (OpenAI), Antigravity (Google) and Claude (Anthropic). The code changes and tests were done with Claude. The app icon is AI-assisted artwork made for this fork (`macos/Assets/AppIcon.png`).

MIT License. See the `LICENSE` file. The original copyright notice and license text are kept as they are.

## Earlier document

[The README from before this revision](REFERENCE.md) is kept unchanged. It has the Windows instructions and the description written for the original. Where the two differ, this README takes precedence. Design records are in `docs/`.
