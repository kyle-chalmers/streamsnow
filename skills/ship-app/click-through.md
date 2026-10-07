# After a green deploy: hand the user the app link

The deploy run passed, `verify-deploy` included. That proves the Streamlit object exists and has a
live version. It cannot prove the pages render or the numbers are right, and CI cannot load the
page to find out: the CI user signs in by key pair, and Snowsight needs a person. So the ship ends
with the user clicking through the deployed app. A failed deploy run never gets here; report it
as SKILL.md's "A check fails" says.

1. **Get the link:** `streamsnow app-url <slug>` prints the Snowsight URL of the deployed app. It
   asks the user's own connection for the organization and account (one read-only `SELECT`), so
   the URL names both: give it to the user in this conversation only, never in the PR, an issue
   or a commit.
2. **Hand it over with a short click-through checklist:**
   - open every page;
   - change each filter once;
   - confirm the numbers load and match what preview showed.

   Anything off is a fix in a new `/ship-app` run, never a hand edit in Snowsight.
3. **Exit 2** (no link: the error line says why, often a connection `streamsnow doctor` can
   check) → do not build a URL by hand. Tell the user where the app is in Snowsight: **Projects »
   Streamlit**, titled after the slug (`<slug>` in Title Case), in the app database and schema
   from `streamsnow config-get snowflake.objects.app_database` and `snowflake.objects.app_schema`.
