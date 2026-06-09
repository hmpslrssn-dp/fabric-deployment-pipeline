# Fabric Deployment Pipeline

A reusable GitHub Actions pipeline for deploying Power BI Reports and Semantic Models to Microsoft Fabric workspaces. All deployment logic lives in this central repo — content repos each call it with a single thin workflow, so improvements here are picked up everywhere automatically.

---

## How it works

This repo follows a **multi-repo pattern**:

```
fabric-deployment-pipeline/          ← this repo (central)
  .github/workflows/deploy.yml       ← reusable workflow (called by content repos)
  scripts/deploy.py                  ← Python deployment logic
  examples/content-repo-workflow.yml ← template to copy into each content repo

pbi-sales-reports/                   ← example content repo
  .github/workflows/deploy.yml       ← thin workflow that calls the central one
  artifacts/
    reports/
      sales-report.Report/
    semantic-models/
      Sales-model.SemanticModel/
      parameter.yml
```

Each content repo has three long-lived branches — **dev**, **test**, and **prod** — that map directly to three Fabric workspaces. A push to any of those branches automatically deploys the content on that branch to the matching workspace.

```
push to dev  →  deploy to dev  Fabric workspace
push to test →  deploy to test Fabric workspace
push to prod →  deploy to prod Fabric workspace  (requires manual approval)
```

The central repo needs to be on `main` so content repos can reference it at `@main`. The `main` branch of this repo always contains the latest stable deployment logic.

---

## Prerequisites

Before setting up any content repo you need:

1. **An Azure service principal** with access to your Fabric workspaces (see [Create a service principal](#1-create-an-azure-service-principal) below).
2. **Three Fabric workspaces** — one each for dev, test, and prod — with the service principal added as a **Member** or **Admin** on each.
3. **This repo set to public** (or "Accessible from repositories in the organisation" enabled in Settings → Actions → General → Access). Content repos use `GITHUB_TOKEN` as the checkout credential and can only access this repo if it is public or explicitly shared.

---

## One-time setup

### 1. Create an Azure service principal

A service principal is an automated identity that the pipeline uses to authenticate to Fabric. You only need to create one — it is shared across all content repos.

1. In the [Azure Portal](https://portal.azure.com), open **Microsoft Entra ID → App registrations → New registration**.
2. Give it a name (e.g. `fabric-deployment-sp`) and register it.
3. Note down the **Application (client) ID** and **Directory (tenant) ID** from the Overview page.
4. Go to **Certificates & secrets → New client secret**. Copy the secret value immediately — it is only shown once.
5. In each Fabric workspace, open **Manage access** and add the service principal as a **Member**.

### 2. Store secrets in GitHub

Secrets can be stored at the **organisation level** (recommended — shared across all repos) or at the individual content repo level.

| Secret name          | Value                                      |
|---------------------|--------------------------------------------|
| `AZURE_CLIENT_ID`   | Application (client) ID from step 1        |
| `AZURE_CLIENT_SECRET` | Client secret value from step 1          |
| `AZURE_TENANT_ID`   | Directory (tenant) ID from step 1          |

To add org-level secrets: **GitHub org → Settings → Secrets and variables → Actions → New organisation secret**.  
Select "All repositories" or restrict to specific repos.

---

## Setting up a new content repo

### 1. Create the folder structure

Power BI Desktop's **Save to PBIP format** option generates the right folder layout automatically. Export your report and semantic model in PBIP format, then organise the output like this:

```
artifacts/
  reports/
    My-Report.Report/         ← exported by Power BI Desktop
      definition.pbir
      definition/
        ...
  semantic-models/
    My-Model.SemanticModel/   ← exported by Power BI Desktop
      definition.pbism
      definition/
        ...
    parameter.yml             ← create this manually (see Parameter substitution)
```

> **Tip:** When saving as PBIP in Power BI Desktop, you get a `.pbip` project file alongside the item folders. You can commit the item folders (`.Report/`, `.SemanticModel/`) to the repo and ignore the `.pbip` file and the `.pbi/` cache folder — they are local Power BI Desktop artefacts and do not affect deployments.

### 2. Connect the report to the semantic model using `byPath`

Open `artifacts/reports/My-Report.Report/definition.pbir` and make sure `datasetReference` uses the `byPath` format:

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
  "version": "4.0",
  "datasetReference": {
    "byPath": {
      "path": "../My-Model.SemanticModel"
    }
  }
}
```

**Why `byPath` and not `byConnection`?**  
Power BI Desktop sometimes saves a `byConnection` entry with a hardcoded connection string pointing at your local dev workspace. If that gets deployed to test or prod the report will still be reading from dev. `byPath` uses a relative path instead — when the pipeline deploys, `fabric-cicd` resolves the path to whichever semantic model was just deployed in the target workspace and rewrites the connection automatically.

The path `../My-Model.SemanticModel` means: go up one level from the report item folder and then into the semantic model folder. It reflects how the deployment script stages the items — it copies all `.Report` and `.SemanticModel` folders into a single flat temporary directory before deploying, so both items sit side-by-side at the same level.

> **Watch out:** If you export your report again from Power BI Desktop it will overwrite `definition.pbir` with a new `byConnection` entry. Re-apply the `byPath` change before pushing.

### 3. Add the workflow file

Copy `examples/content-repo-workflow.yml` from this repo into your content repo at `.github/workflows/deploy.yml` and update the two placeholders:

```yaml
# Replace this:
uses: your-org/fabric-deployment-pipeline/.github/workflows/deploy.yml@main

# With your actual org name:
uses: hmpslrssn-dp/fabric-deployment-pipeline/.github/workflows/deploy.yml@main
```

The `reports_path` and `semantic_models_path` inputs default to `artifacts/reports` and `artifacts/semantic-models` — only change them if your folder structure differs.

### 4. Create GitHub Environments

In the content repo, go to **Settings → Environments** and create three environments named exactly **`dev`**, **`test`**, and **`prod`**.

For each environment:
- Add a variable: `FABRIC_WORKSPACE_ID` = the GUID of the matching Fabric workspace.  
  (Find the workspace GUID in the Fabric URL: `app.powerbi.com/groups/<GUID>/...`)
- On **prod** only: add a required reviewer under **Protection rules → Required reviewers**. This pauses the deployment and waits for a manual sign-off before proceeding.

Make sure the three Azure secrets (`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`) are accessible from this repo — either inherited from the org or added directly under **Settings → Secrets and variables → Actions**.

### 5. Create the branches and push

```bash
git checkout -b dev   && git push -u origin dev
git checkout -b test  && git push -u origin test
git checkout -b prod  && git push -u origin prod
```

Push a change to the `dev` branch to trigger the first deployment.

---

## Parameter substitution

Use `parameter.yml` to swap values inside Semantic Model definition files depending on which environment is being deployed to. Place the file at `artifacts/semantic-models/parameter.yml`.

There are two substitution strategies:

### `key_value_replace` (recommended)

Targets a specific key in a JSON or TMDL file using a [JSONPath](https://goessner.net/articles/JsonPath/) expression. Only that key's value is changed — nothing else in the file is affected even if the same string appears elsewhere.

```yaml
key_value_replace:
  - find_key: "$.connectionDetails.server"
    replace_value:
      dev:  "dev-sql.database.windows.net"
      test: "test-sql.database.windows.net"
      prod: "prod-sql.database.windows.net"
```

To scope a rule to a specific model, add `item_name`:

```yaml
key_value_replace:
  - find_key: "$.connectionDetails.database"
    replace_value:
      dev:  "dev_sales_db"
      test: "test_sales_db"
      prod: "prod_sales_db"
    item_name: "My-Model"
```

### `find_replace`

Plain text search-and-replace across an entire file. Use this only when the value is embedded in a free-text blob that has no accessible JSON key (for example, a Power BI parameter stored as a TMDL table).

```yaml
find_replace:
  - find_value: "dev-value-to-replace"
    replace_value:
      test: "test-replacement-value"
      prod: "prod-replacement-value"
    file_path: "My-Model.SemanticModel/definition/tables/my_parameter.tmdl"
```

**Important:** `file_path` is relative to the deployment staging directory root (not to the item folder). Always prefix it with the item folder name:

```
My-Model.SemanticModel/definition/tables/my_parameter.tmdl
^^^^^^^^^^^^^^^^^^^^^^^
item folder name — required prefix
```

**Tip:** If you only need a value to change in test and prod but not in dev, you can omit the `dev` key entirely. The dev value is whatever is committed to the `dev` branch — the pipeline only substitutes it when deploying to test or prod.

---

## Deployment flow

```
developer pushes to dev branch
         │
         ▼
  gate job runs
  (reads FABRIC_WORKSPACE_ID from the "dev" environment variable,
   passes it as an output to the next job)
         │
         ▼
  deploy job runs (calls this repo's reusable workflow)
    1. checks out content repo  →  content/
    2. checks out this repo     →  deployment/
    3. installs Python deps from deployment/requirements.txt
    4. runs deployment/scripts/deploy.py:
         a. copies .SemanticModel folders  ┐
         b. copies .Report folders         ├─ into a temp staging dir
         c. copies parameter.yml           ┘
         d. deploys semantic models (with parameter substitution)
         e. deploys reports (auto-rebound to the correct semantic model)
         f. deletes staging dir
```

---

## Troubleshooting

**`Secret GH_PAT is required, but not provided`**  
Make sure your content repo workflow passes `GH_PAT: ${{ secrets.GITHUB_TOKEN }}` explicitly under `secrets:`. Do not use `secrets: inherit`.

**`Not Found` when checking out the deployment repo**  
This repo must be public, or "Accessible from repositories in the organisation" must be enabled under Settings → Actions → General → Access. `GITHUB_TOKEN` from a content repo cannot access a private repo that belongs to a different repo.

**`FABRIC_WORKSPACE_ID is not set`**  
Environment-level variables are only accessible in jobs that have `environment:` set. The deploy job uses `uses:` for the reusable workflow and cannot also use `environment:`. The gate job reads `vars.FABRIC_WORKSPACE_ID` and passes it as `outputs.workspace_id` — make sure your content repo workflow's gate job has `outputs: workspace_id: ${{ vars.FABRIC_WORKSPACE_ID }}` and the deploy job references it as `workspace_id: ${{ needs.gate.outputs.workspace_id }}`.

**`Error loading parameter.yml`**  
Check the YAML is valid — no tabs, no `[]` after the top-level key, and list items indented correctly under their parent key. The `file_path` inside each rule must be relative to the staging root (i.e. start with the item folder name, not `definition/`).

**`Semantic model not found in the repository`**  
This means `definition.pbir` uses `byPath` but the report's workspace instance can't find the semantic model. It usually means the `byPath` path is wrong. Check that the path in `definition.pbir` is `../My-Model.SemanticModel` (one level up, then the model folder name). The `..` navigates from inside the report item folder up to the staging root where the model folder sits.

**`The report is still pointing at the wrong workspace after deployment`**  
Open `definition.pbir` and confirm it uses `byPath`, not `byConnection`. Power BI Desktop may have overwritten it with a hardcoded `byConnection` string the last time you saved the report — re-apply the `byPath` change and push again.

---

## Repository structure

```
.github/
  workflows/
    deploy.yml          reusable workflow (called by content repos via workflow_call)
examples/
  content-repo-workflow.yml   template — copy this into each content repo
scripts/
  deploy.py             Python script that performs the actual deployment
requirements.txt        Python dependencies (fabric-cicd, azure-identity)
```
