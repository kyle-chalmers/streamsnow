# After merge: clean up the spent branch

The PR is merged. Remove the branch that shipped, so the next ship starts from `main` and nobody
reuses a squash-merged branch by accident. /ship-app itself never merges; this only tidies up
after a merge someone else made.

Two things start it:

- **The checks watch in this run reports the PR as `MERGED`.** Run the steps below from the
  shipped branch. The checkout ends on updated `main`.
- **A later `/ship-app` run, at step 4, finds the current branch already squash-merged.** That run
  has app changes to ship (`/ship-app` step 1 requires them), so the tree is dirty or the branch
  holds commits made after the merge, and the checks below would stop it. Move the work first, as
  step 4 says: a fresh `ship/...` branch off `main`, with the edits copied or the commits
  cherry-picked. Run the steps below only after `/ship-app` step 6 has committed the work on the
  new branch, which is when the tree is clean again. This path stays on the new branch (skip step
  5 below), so on it `<branch>` in every step below is the spent branch, not the current one. Any stop below ends the cleanup only: leave the spent branch in place, say why, and
  continue the ship at `/ship-app` step 7.

## Whether to ask first

Approval in this run means that during this `/ship-app` run the user explicitly approved in this
conversation (not a GitHub review approval) the PR merging, merged it themselves and said so, or told you to let it merge and the watch then saw
`MERGED`. With that approval, skip the question in step 4 below and report the cleanup afterward:
approving the merge already means the branch is done, and deleting a merged branch is recoverable
(its commits are in `main`, GitHub's Restore branch button, the reflog). The step-4 trigger, or a
merge the watch saw without the user's say-so, has no approval: ask once.

Steps 1 to 3 run on both paths, before anything is deleted.

## Steps, in this order

1. **Require a clean working tree** (`git status --short` prints nothing). Anything else: stop and
   say why. Uncommitted work on a spent branch is the user's to place first.
2. **Confirm the PR is merged:** `gh pr view <num> --json state,headRefOid` must read `MERGED`.
   Anything else: stop, nothing below runs. Keep `headRefOid`, the commit the PR merged.
3. **Check the branch holds nothing newer than the merge.** A later run can find commits made
   after the PR merged, and `-D` would destroy them. Both tips must equal `headRefOid`:
   - the local tip, `git rev-parse <branch>`;
   - the remote tip, from `git ls-remote --heads origin <branch>`: the first field of the line
     whose ref is exactly `refs/heads/<branch>`, never a line that only ends in `<branch>` (that
     suffix also matches `refs/heads/x/<branch>`). No such line means the remote branch is
     already gone, which is fine.

   On any mismatch, stop and delete nothing. Name the extra commits with
   `git log --oneline <headRefOid>..<branch>` (or against `origin/<branch>` for the remote tip)
   and tell the user the branch has unmerged work. On the step-4 path that includes commits you
   cherry-picked to the new branch: say so, and leave the spent branch for the user to delete.
4. **Ask the user once, only when there was no approval in this run**, naming both the local
   branch and the remote branch it will remove. Declined: stop and leave both in place. With
   approval, go straight on and report in step 9.
5. **Update `main`** (watch path only): `git switch main`, then `git pull --ff-only`. A
   fast-forward that cannot happen means local `main` has commits of its own: stop and report it.
6. **Delete the local branch:** `git branch -D <branch>`. The `MERGED` state from step 2 and the
   matching tip from step 3 are the evidence that make `-D` safe: a squash merge leaves the
   branch's commits unreachable from `main`, so `git branch -d` refuses even though the work
   shipped.
7. **Delete the remote branch only if it is still there:** when step 3's
   `git ls-remote --heads origin <branch>` listed it, run `git push origin --delete <branch>`.
   The repo's auto-delete-on-merge setting may already have removed it, and deleting a missing
   ref is an error, not a no-op.
8. **Tidy remote-tracking refs:** `git fetch --prune`.
9. **Report** what was deleted (local, remote or both) and where the checkout is, for example
   "Cleaned up: deleted `<branch>` locally (GitHub already removed it), now on `main` at `<sha>`."

Never run this for an open or closed-unmerged PR, and never widen it to other branches: sweeping
many merged branches is a separate job.
