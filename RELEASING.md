# Releasing healwright

healwright ships three things from this repo, and each one reaches people by a different ref:

| What | How people get it | Which ref they get |
|---|---|---|
| Skills (Claude Code plugin) | a marketplace: this repo's own, `kyle-chalmers/claude-plugins`, or `kyle-chalmers/kyle-chalmers-skills` | the default branch, whatever the tags say |
| `fix.yml` (reusable workflow) | `uses: kyle-chalmers/healwright/.github/workflows/fix.yml@v1` in a caller | the floating `v1` tag |
| `templates/healer/` | copied by hand into a jobs repo | whatever they cloned |

So "released" means three refs agree: `main`, the version tag, and `v1`. This file is the
checklist that gets them to agree. Read [`AGENTS.md`](AGENTS.md) and
[`docs/status.md`](docs/status.md) first.

## Release state today (2026-10-27)

They do not agree yet.

| Item | Where it points | What that means |
|---|---|---|
| `CHANGELOG.md` | `## 0.1.0 (unreleased)` | Describes everything on `main`, including the sixth review pass and the skills rework (#1). |
| Tag `v0.1.0` | `32b64ac` (third commit) | Older than everything the CHANGELOG describes from `8285cbe` on. Its `fix.yml` sets `runner.temp` in job-level `env`, the revision GitHub rejected (see `docs/status.md`), and still gives the model `Bash` and `WebFetch`, which the sixth pass (`e005cfc`) removed. Do not point anyone at it. |
| Tag `v1` | `6353164` | Has the final `fix.yml`; `main` has changed only `ci.yml` under `.github/` since. Missing the skills and docs from #1. CI on that commit failed once (`selftest (3.10)`); `main` is green. |
| `.claude-plugin/plugin.json`, `pyproject.toml`, `healwright_core.__version__`, README Status | `0.1.0` | Consistent with each other. |
| GitHub Release | none | |
| Marketplace listings | all three list `healwright` from the default branch | Plugin users already get `main`. |

## Decision before the first release (maintainer only)

The `v0.1.0` tag does not contain what CHANGELOG 0.1.0 describes. Pick one:

- **A. Leave `v0.1.0` where it is and release `main` as 0.1.1.** No published ref moves. The
  CHANGELOG gets a `## 0.1.1 (YYYY-MM-DD)` heading over today's 0.1.0 text and a one-line
  `## 0.1.0` entry saying the tag is superseded and its `fix.yml` does not run.
- **B. Move `v0.1.0` to the release commit.** Cleaner history, but anyone who already fetched
  the tag keeps the old commit silently. Only safe if you are sure nobody has.

Either way, `v1` moves to the release commit (step 6 below). That is how a floating major tag
works, and it changes the workflow every caller runs the moment it is pushed.

## Gate before tagging

Every item is a box to tick in the release PR.

1. **`fix.yml` end to end.** Run `docs/status.md` section 1 in a throwaway repo with a throwaway
   GitHub App, including the negative test. Record the run URL in `docs/status.md`. This blocks
   the release: callers point at `fix.yml@v1` and nothing about it has run yet.
2. **Live Databricks items.** Either run the minimum set in `docs/status.md` section 2 (stage A
   items 1 to 3 and 14 to 16), or confirm README Status still lists each one as open. Shipping
   with them open is allowed; shipping while the README implies they passed is not (`AGENTS.md`
   tiebreaker 8).
3. **The gate, with the private denylist.**
   ```bash
   HEALWRIGHT_LEAK_DENYLIST=~/.config/healwright-denylist.txt bash bin/selftest.sh
   ```
4. **CI green on `main`** for every job: `selftest` on 3.10, 3.11 and 3.12, `workflow-security`,
   and `plugin-validate`.
5. **Two independent read-only reviews** of the diff since the last release: one for code, one
   only for organisation or domain leakage (`CONTRIBUTING.md`).

## Cut the release

1. **Bump the version in lockstep** in all four places, so the plugin, the template and the docs
   agree about what is installed:
   - `.claude-plugin/plugin.json` (`version`)
   - `templates/healer/healwright_core.py` (`__version__`)
   - `pyproject.toml` (`version`)
   - README `## Status` (the bold version line)
2. **Close the changelog.** Replace `(unreleased)` with the date: `## X.Y.Z (YYYY-MM-DD)`.
3. **Update `docs/status.md`**: the date in its title and anything the gate above verified.
4. **Merge the release PR** and wait for CI on `main`.
5. **Tag the merged commit** with an annotated tag and the fully-qualified refspec:
   ```bash
   git tag -a vX.Y.Z -m "healwright X.Y.Z"
   git push origin refs/tags/vX.Y.Z
   ```
6. **Move `v1`** to the same commit, only once steps 4 and 5 are done:
   ```bash
   git tag -fa v1 -m "healwright v1 (X.Y.Z)" vX.Y.Z^{}
   git push --force origin refs/tags/v1
   ```
7. **Create the GitHub Release** from `vX.Y.Z`, with the CHANGELOG section as its notes.

## Verify

1. `git ls-remote --tags origin` shows `vX.Y.Z^{}` and `v1^{}` on the same commit as `main`.
2. In a scratch project, `claude plugin marketplace add kyle-chalmers/claude-plugins`, then
   `claude plugin install healwright@kyle-chalmers`: the three skills appear.
3. The healwright description reads the same in this repo's `.claude-plugin/marketplace.json`,
   `kyle-chalmers/claude-plugins` and `kyle-chalmers/kyle-chalmers-skills`. A change there is a
   PR in that repo, not here.
