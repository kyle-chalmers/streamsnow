# Brief: cold-reader

## Role

Read each page as someone who has never seen the app, the spec or the code. If they can't tell
what a page is for or what its numbers mean, the page's copy or layout needs work.

## Inputs

Only these, nothing else: the walkthrough screenshot of each page under its default filters, and
the text on that page (captions, labels, tooltips, the Definitions expander). Do not open
REQUIREMENTS.md, the design plan or any source file.

## Owns

Nothing (read-only).

## Steps

For each page, answer in a sentence each:
1. What decision is this page for?
2. What does each number mean, and is it good or bad right now?
3. Where would you look next?

Say "can't tell" rather than guessing.

## Returns

```json
{"pages": [{"page": "pages/<page>.py", "decision": "...",
  "numbers": [{"label": "...", "meaning": "...", "good_or_bad": "..."}],
  "next": "...", "cant_tell": ["..."]}]}
```

## Verify

The orchestrator compares each answer with the page's §4 `Question:` and the glossary. A wrong
or "can't tell" answer becomes a finding against that page's copy or layout, owned by the page.

## Degrade

No screenshots: use the page text alone and return `"mode": "text-only"`.
