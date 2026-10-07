# Switching to the Git repository deploy source

StreamSnow deploys with **stage-copy** by default: on each merge, CI uploads the
app files to an internal stage under that commit's SHA and Snowflake builds the
app from there. The alternative is **git-repository**: Snowflake keeps a
[`GIT REPOSITORY`](https://docs.snowflake.com/en/developer-guide/git/git-overview)
object that mirrors your GitHub repo, CI tells it to fetch, and each app is built
from the branch. This guide covers when to switch and how.

## Should you switch?

Stay on stage-copy unless the Git repository's benefits matter to your team.

| | stage-copy (default) | git-repository |
|---|---|---|
| Who talks to whom | CI to Snowflake only | Snowflake also reaches out to github.com |
| One-time objects | an internal stage | an account-level API integration, a `GIT REPOSITORY`, and (private repos) a secret holding a GitHub token |
| Admin rights | `CREATE STAGE` on a schema | `CREATE INTEGRATION`, which only ACCOUNTADMIN has by default ([CREATE API INTEGRATION](https://docs.snowflake.com/en/sql-reference/sql/create-api-integration)) |
| Secrets to rotate | none beyond the CI key pair | a GitHub token for a private repo |
| What a deploy builds from | the exact merged commit (the stage path contains the SHA) | the branch as of the fetch; verify-deploy then checks the commit Snowflake recorded |
| What you gain | fewest moving parts | browse the repo's files, branches and history in Snowsight, and use them from other Snowflake features |

Check these before switching:

- **Network.** Snowflake must reach `https://github.com`. If your GitHub
  organization allowlists IP addresses, Snowflake can route Git traffic through
  stable egress IP addresses on supported cloud providers, and a private link
  is the alternative to the public internet; both need setup on your side
  ([setting up Git](https://docs.snowflake.com/en/developer-guide/git/git-setting-up)).
  A firewall or network policy that blocks this fails every deploy at the
  fetch step.
- **GitHub Enterprise Server.** StreamSnow accepts only `https://github.com/...`
  origins today, so a self-hosted GitHub Enterprise Server is not supported on
  this path. GitHub Enterprise Cloud repos on github.com work.
- **Size.** Repositories over 2 GB, and submodules, are not supported
  ([Git limitations](https://docs.snowflake.com/en/developer-guide/git/git-limitations)).
- **Committed docs are visible.** The stage-copy workflow uploads a bundle from
  `streamsnow stage-bundle` that leaves out each app's root-level docs
  (`AGENTS.md`, `REQUIREMENTS.md` and the like) and `sql_review/`. The Git
  repository mirrors the whole repo instead, and each app is built from its
  committed folder, so anyone who can read the `GIT REPOSITORY` object can read
  those files. Keep anything that should not be visible in Snowflake out of the
  repo. The `stage-files` check in `verify-deploy` does not apply to this
  source and reports itself as skipped.

## 1. Preview the setup SQL

Without changing your config, print the admin bootstrap for the Git path:

```bash
streamsnow deploy-setup --admin --source git-repository \
  --git-origin https://github.com/<owner>/<repo>.git
```

Add `--github-auth public` for a public repo (no token, no secret). The output
starts with a `PREVIEW` banner and is for review only: StreamSnow never runs
SQL. In place of the stage-copy bootstrap's stage and its `CREATE STAGE` grant,
it creates these, named after your app database and schema unless you set them
in config:

- `CREATE API INTEGRATION` (ACCOUNTADMIN section), allowing only
  `https://github.com/<owner>`, not all of GitHub. It is `IF NOT EXISTS`, so an
  integration you created earlier keeps its settings; change those with
  `ALTER API INTEGRATION`.
- `CREATE SECRET` for the token (private repos only).
- `CREATE GIT REPOSITORY` with your repo as its `ORIGIN`, created by the CI
  role so the deploy can fetch it.

## 2. Create a GitHub token (private repos only)

A public repo needs no token: skip to step 3.

For a private repo, create a
[fine-grained personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-fine-grained-personal-access-token)
that can only read this one repository:

- **Resource owner:** the organization or account that owns the repo.
- **Repository access:** only the app repository.
- **Repository permissions:** Contents: Read-only (Metadata: Read-only is added
  automatically). Nothing else.
- **Expiration:** pick one your team will rotate. When it expires, fetches fail
  until the secret is updated with
  `ALTER SECRET <secret> SET PASSWORD = '<new token>'`.

Paste the token in place of `<github-token>` in the `CREATE SECRET` statement
when you run it. Never commit it, and never put it in `streamsnow.config.yaml`.
A token owned by a machine account outlives any one person's access. Snowflake
also offers a GitHub App connection for Git workspaces; StreamSnow's CI uses a
token secret because the deploy runs without a person present.

## 3. Switch the config

1. Run `streamsnow configure` and choose `git-repository` as the deploy source.
2. Add your repo's URL under `deploy:` in `streamsnow.config.yaml`, and
   `github_auth_mode: public` if the repo is public:

   ```yaml
   deploy:
     source: "git-repository"
     git_origin: "https://github.com/<owner>/<repo>.git"
     github_auth_mode: "pat"   # or "public" for a public repo
   ```

3. Print the real setup, have an admin review and run it, then re-render the
   deploy workflow:

   ```bash
   streamsnow deploy-setup --admin     # admin reviews and runs it once
   streamsnow update                   # dry run: shows deploy.yml will change
   streamsnow update --apply           # writes the git-repository deploy.yml
   ```

4. Commit the config and the new `.github/workflows/deploy.yml` in a PR and merge
   it. The `SNOWFLAKE_*` repo secrets stay the same.

## 4. Check the first deploy

On merge, the workflow runs `snow git fetch` (a failed fetch fails the deploy),
then `CREATE OR REPLACE STREAMLIT ... FROM '@<repo>/branches/<branch>/apps/<slug>/'`
for each app. `CREATE` copies the files once, so a later push changes nothing
until the next deploy
([CREATE STREAMLIT](https://docs.snowflake.com/en/sql-reference/sql/create-streamlit)).

`streamsnow verify-deploy <slug> --sha <merge sha>` then reads
`DESCRIBE STREAMLIT` and confirms `last_version_git_commit_hash` is the merged
commit ([DESCRIBE STREAMLIT](https://docs.snowflake.com/en/sql-reference/sql/desc-streamlit)).
A different hash means the branch moved before the fetch (the newer commit's own
deploy ships it) or the fetch did not pick up the merge.

Snowflake does not accept a `/commits/<sha>/` path as a Streamlit source from a
Git repository ("Invalid git branch path"), which is why the deploy builds from
the branch and proves the commit afterwards.

## 5. Clean up the old stage

Once a Git deploy has passed verify-deploy, the stage-copy stage is unused. It
holds every previously deployed commit, so keep it until you no longer need
those for rollback, then drop it as the CI role:

```sql
DROP STAGE IF EXISTS <stage_database>.<stage_schema>.<stage_name>;
```

The stage name is `snowflake.objects.stage_name` in your config
(`streamsnow stage-path` prints the full path).

## Switching back

Run `streamsnow configure`, choose `stage-copy`, then
`streamsnow deploy-setup --admin` (for the stage) and `streamsnow update --apply`.
The Git objects can stay or be dropped; nothing reads them once the workflow
deploys from the stage.
