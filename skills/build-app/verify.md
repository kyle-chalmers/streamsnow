# Verify — three reviewers, bounded fixes, then CHECKPOINT 2

After the build, three advisory reviewers look at the running app in parallel. Their findings go
back to whoever owns the file; nothing here is a gate (`streamsnow validate-app` is). Dispatch each
with its brief ([briefs/](briefs/)); without subagents, run them yourself one after another
([_shared/other-agents.md](../_shared/other-agents.md)).

## Round

1. Start the preview (`streamsnow preview start <slug>`) and run the UI walkthrough
   ([_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md)) for one screenshot
   per page under default filters.
2. Dispatch in one message: **perf-reviewer** ([briefs/perf-reviewer.md](briefs/perf-reviewer.md)),
   **viz-critic** ([briefs/viz-critic.md](briefs/viz-critic.md)) and **cold-reader**
   ([briefs/cold-reader.md](briefs/cold-reader.md)). Give cold-reader only the screenshots and the
   page text.
3. Turn cold-reader's answers into findings: an answer that misses the page's §4 `Question:`, or
   misreads a number against its glossary entry, is a should-fix on that page's copy or layout.
4. Merge all findings (same file and line → one finding, sources listed) and route them by
   `owner`: page findings to a page-builder in `fix` mode (several pages in parallel); shared-file
   findings (`pages/_*.py`, `streamlit_app.py`, shared queries) to yourself. Then
   `streamsnow sql-review generate <slug>` and `streamsnow sql-review check <slug>` once.

## Bound

At most **two** fix rounds. Re-run only the reviewers that had findings. Whatever is still open
after round two goes to the user at CP2 with its reason; never loop a third time on your own.

## CHECKPOINT 2

5. Hand over the preview URL and the open findings (if any). The user clicks through every page:
   pages render, charts populate, filters work. Block until they answer.
