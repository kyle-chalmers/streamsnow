# After merge: clean up the spent branch

The PR is merged. Leave the checkout on updated `main` and remove the branch that shipped, so the
next ship starts from `main` and nobody reuses a squash-merged branch by accident.

Two things start it:

- the checks watch in this run reports the PR as `MERGED`;
- a later `/ship-app` run, at step 4, finds the current branch already squash-merged.

## Steps, in this order

1. **Require a clean working tree** (`git status --short` prints nothing). Anything else: stop and
   say why. Uncommitted work on a spent branch is the user's to place first.
2. **Confirm the PR is merged:** `gh pr view <num> --json state` must read `MERGED`. Anything
   else: stop, nothing below runs.
3. **Ask the user once**, naming both the local branch and the remote branch it will remove.
   Declined: stop and leave both in place.
4. **Update `main`:** `git switch main`, then `git pull --ff-only`. A fast-forward that cannot
   happen means local `main` has commits of its own: stop and report it.
5. **Delete the local branch:** `git branch -D <branch>`. The `MERGED` state from step 2 is the
   evidence that makes `-D` safe: a squash merge leaves the branch's commits unreachable from
   `main`, so `git branch -d` refuses even though the work shipped.
6. **Delete the remote branch only if it is still there:** run
   `git ls-remote --heads origin <branch>`; when it lists the branch, run
   `git push origin --delete <branch>`. The repo's auto-delete-on-merge setting may already have
   removed it, and deleting a missing ref is an error, not a no-op.
7. **Tidy remote-tracking refs:** `git fetch --prune`.
8. **Report** what was deleted (local, remote or both) and that the checkout is on `main`.

Never run this for an open or closed-unmerged PR, and never widen it to other branches: sweeping
many merged branches is a separate job.
