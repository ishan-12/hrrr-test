# AI prompt log

Every prompt sent to Claude Code in this repo, appended automatically by
[.claude/hooks/log-prompt.sh](../.claude/hooks/log-prompt.sh) (a `UserPromptSubmit` hook). Messages from
Claude's own helper agents are not logged. Source material for write-up question 2.

### 2026-11-12T11:49:37-08:00 · session 683dd8e4

> <pasted_content id="c1d2">
> Build hrrr-ingest by following the agreed plan in notes/hrrr-brief.consolidated.html. Read it in full
> first: the "The plan" tab is the source of truth; the challenge spec is comity-coding-challenge-weather.md.
>
> Ground rules:
> 1. llm-rule-engine is active in this repo. First run `ruler doctor` and resolve anything it reports
>    before writing code. Every change goes through its preflight and evidence gates. Never edit
>    .llm-rule-engine.json or the Claude settings files, and never weaken a gate. Ignore the plan's cut-list
>    item "set R9 to warn".
>    - Tests: run exactly `uv run pytest …`.
>    - Mutation testing: run exactly `mutmut run --CI --paths-to-mutate <changed file>` (plain mutmut, or
>      via `uv run`), on every changed source file in the mutation scope.
>      - Runs take minutes, so start them in the background and cite one only after it has finished.
>      - After any interrupted run, check `git diff` for a leftover mutation before continuing.
>      - If a turn deletes a source file, use an unscoped `mutmut run --CI`.
>    - Survivors: if R9 lists surviving mutants, kill them with a better test where you can. For any you
>      believe are equivalent, show me each one (`mutmut show <id>` and why no input the program can
>      receive distinguishes it), then wait for my reply. You cannot approve them yourself.
>    - If the same gate blocks a turn twice, stop and tell me what's blocking.
> 2. Plan steps 1-3:
>    - Step 1 is done (tools installed, engine enabled).
>    - For step 2, run only its leak scan; the files it mentions editing don't exist in this repo.
>    - Step 3 (docs/worklog.md) is mine: don't create or edit it.
> 3. D1-D5: implement the plan's recommended answers; I accept them. Write docs/adr/0001-… yourself
>    (step 5) and mark it "drafted by Claude; owner's wording pending".
> 4. The plan reserves some code for me to hand-write (the variable registry, the CLI signature, the table
>    DDL and the missing-key SQL). I'm delegating it to you: write it, in its own commits. Every commit
>    keeps the Co-Authored-By trailer. Never state or imply that I wrote code.
> 5. Read and write only inside this repository, plus the public NOAA bucket: listings, .idx files and
>    byte ranges, never a whole GRIB2 file. Don't list or open anything else under ~/git-main or
>    ~/.claude, and give the same rule to every helper agent you start.
>    - Treat the plan's variable-identity table as the claim to check, not the answer: build the registry
>      from your own step-4 decodes, then check they agree.
>    - The full default live run fetches about 0.8 GB (576 GETs) and takes several minutes; run it once.
> 6. Commit at the end of each phase. Before claiming something works, show the command and its output.
> 7. When the implementation is complete, have three independent reviewer agents review it against the
>    plan and spec: weather data, correctness and testing, and the grader's view. Fix what they find
>    until all three agree.
> 8. Finish with:
>    - plan steps 12-15 (README, WRITEUP.md draft with [OWNER] sections for Q1/Q2, Dockerfile, final
>      check);
>    - `ruler report` saved to notes/ruler-report.txt; commit a copy only if it passes the step-2 leak scan;
>    - a summary in WRITEUP.md of the gated turns, the blocks, the survivors I approved, and every time
>      the engine blocked or changed something;
>    - `ruler report` must exit 0. If it doesn't, say so plainly.
> </pasted_content id="c1d2">
