# =============================================================================
# deploy.py
#
# This script is called by the GitHub Actions reusable workflow to deploy
# Power BI Reports and Semantic Models to a Fabric workspace.
#
# It uses the `fabric-cicd` Python package, which is Microsoft's open-source
# library for automating Fabric deployments. Under the hood it calls the
# Fabric REST API on your behalf, handling authentication and item publishing.
#
# In the multi-repo setup, this script lives in the central deployment repo
# but runs against artifact files checked out from a content repo. The paths
# to those artifact folders are passed in as environment variables by the
# workflow — the script itself doesn't need to know or care which content repo
# it's deploying from.
#
# The script reads all configuration from environment variables injected by
# the GitHub Actions workflow — it contains no hardcoded credentials, workspace
# IDs, or file paths, making it safe to commit to source control.
#
# WHY A COMBINED DIRECTORY?
# -------------------------
# fabric-cicd resolves "byPath" references in report definition files by
# looking up the referenced semantic model in the same FabricWorkspace
# instance's item registry. That registry is built from a single
# repository_directory — it only knows about items inside that one folder.
#
# Our content repos store reports and semantic models in separate sub-folders
# (artifacts/reports/ and artifacts/semantic-models/). If we point two
# separate FabricWorkspace instances at those separate folders, the reports
# workspace has no knowledge of the semantic models workspace's items, so
# byPath lookups fail with "Semantic model not found in the repository."
#
# The solution is to copy all items we want to deploy into a single temporary
# directory before deploying, then point ONE FabricWorkspace instance at that
# combined directory. Both semantic models and reports are now in the same
# registry, so the byPath lookup succeeds and fabric-cicd can automatically
# rebind the report to the correct semantic model in the target workspace.
# =============================================================================

import os
import sys
import shutil
import tempfile
from pathlib import Path

# fabric_cicd is the Microsoft package that handles communication with the
# Fabric REST API. FabricWorkspace represents the target workspace and
# publish_all_items deploys everything in scope to it.
from fabric_cicd import FabricWorkspace, publish_all_items

# azure-identity provides authentication building blocks. ClientSecretCredential
# implements the OAuth 2.0 "client credentials" flow — the standard way for
# automated pipelines to authenticate without a human signing in interactively.
from azure.identity import ClientSecretCredential


# =============================================================================
# 1. Read configuration from environment variables
# =============================================================================
# os.environ["KEY"] reads a value set in the environment before this script
# was launched. The GitHub Actions workflow sets all of these from GitHub
# Secrets and Variables — they are never written into this file.
#
# REPORTS_PATH and SEMANTIC_MODELS_PATH are the full absolute paths to the
# artifact folders on the runner, constructed by the workflow from the
# github.workspace path and the content repo's folder structure.
#
# If a required variable is missing we print a clear error and exit with
# sys.exit(1), which signals failure to GitHub Actions and marks the run red.
# "Fail fast" is better than letting the script crash deeper in the code with
# a confusing error message.

required_vars = [
    "AZURE_CLIENT_ID",        # The service principal's unique application ID
    "AZURE_CLIENT_SECRET",    # The service principal's password / secret key
    "AZURE_TENANT_ID",        # Your organisation's Azure Active Directory tenant ID
    "FABRIC_WORKSPACE_ID",    # The GUID of the Fabric workspace we're deploying into
    "ENVIRONMENT_NAME",       # The branch / environment name: dev, test, or prod
    "REPORTS_PATH",           # Absolute path to the reports artifacts folder
    "SEMANTIC_MODELS_PATH",   # Absolute path to the semantic models artifacts folder
]

missing = [var for var in required_vars if not os.environ.get(var)]
if missing:
    print(f"ERROR: The following required environment variables are not set: {missing}")
    print("These should be configured in the GitHub Actions workflow.")
    sys.exit(1)

client_id             = os.environ["AZURE_CLIENT_ID"]
client_secret         = os.environ["AZURE_CLIENT_SECRET"]
tenant_id             = os.environ["AZURE_TENANT_ID"]
workspace_id          = os.environ["FABRIC_WORKSPACE_ID"]
environment_name      = os.environ["ENVIRONMENT_NAME"]   # e.g. "dev", "test", "prod"
reports_path          = Path(os.environ["REPORTS_PATH"])
semantic_models_path  = Path(os.environ["SEMANTIC_MODELS_PATH"])


# =============================================================================
# 2. Validate the artifact folders exist
# =============================================================================
# Before attempting a deployment, confirm that the folders the workflow pointed
# us at actually exist on the runner. If they don't, it usually means the
# content repo doesn't follow the expected folder convention, or the
# reports_path / semantic_models_path inputs were set incorrectly.

for folder, label in [(reports_path, "Reports"), (semantic_models_path, "Semantic Models")]:
    if not folder.exists():
        print(f"ERROR: {label} folder not found at: {folder}")
        print("Check that the reports_path and semantic_models_path inputs in your")
        print("calling workflow match the actual folder structure in your content repo.")
        sys.exit(1)

# Check for a parameter file alongside the semantic models.
# fabric-cicd looks for parameter.yml inside the repository_directory by
# default, so placing it in the semantic-models folder is the natural location.
# If it doesn't exist the deployment still runs — parameterisation is optional.
parameter_file_path = semantic_models_path / "parameter.yml"

if parameter_file_path.exists():
    print(f"Parameter file found: {parameter_file_path}")
else:
    print("No parameter file found — deploying without parameter substitution.")


# =============================================================================
# 3. Authenticate with Azure / Fabric
# =============================================================================
# ClientSecretCredential implements the OAuth 2.0 "client credentials" flow.
# It does not log in interactively — instead it uses the client ID and secret
# to obtain a short-lived access token from Azure on demand. fabric-cicd calls
# this credential object whenever it needs to make an API request, and the
# library handles token renewal automatically when tokens expire.

print(f"Authenticating with Azure for environment: {environment_name}...")

credential = ClientSecretCredential(
    tenant_id=tenant_id,
    client_id=client_id,
    client_secret=client_secret,
)


# =============================================================================
# 4. Build a combined staging directory
# =============================================================================
# fabric-cicd requires all items it needs to cross-reference to be in the
# same repository_directory. Because our repo stores reports and semantic
# models in separate sub-folders, we create a temporary flat directory that
# contains only the deployable item folders from both locations.
#
# We deliberately copy only folders whose names end in ".SemanticModel" or
# ".Report" — this matches fabric-cicd's own item-folder naming convention
# and naturally excludes everything else (loose .pbip project files, .pbi
# cache folders, localSettings.json, etc.) that is not a deployable item.
#
# The parameter.yml file is copied alongside the items because fabric-cicd
# always looks for it at the root of repository_directory.
#
# tempfile.mkdtemp() creates an empty directory in the OS temp space and
# returns its path. We clean it up in the finally block whether the
# deployment succeeds or fails.

print("Building combined staging directory for deployment...")

staging_dir = tempfile.mkdtemp(prefix="fabric_deploy_")

try:
    # ── Copy semantic model item folders ──────────────────────────────────
    # Walk the semantic-models source directory and copy each folder that
    # looks like a deployable item (name ends with ".SemanticModel").
    # The "." in the name is what fabric-cicd uses to identify item folders
    # (e.g. "Sales-model.SemanticModel"). Folders without a "." in the name
    # (like a loose sub-folder) or with a different suffix are skipped.
    models_copied = []
    for item in semantic_models_path.iterdir():
        if item.is_dir() and item.name.endswith(".SemanticModel"):
            dest = Path(staging_dir) / item.name
            shutil.copytree(str(item), str(dest))
            models_copied.append(item.name)
            print(f"  Staged semantic model: {item.name}")

    # ── Copy report item folders ───────────────────────────────────────────
    # Same pattern for reports. Only ".Report" suffix items are copied.
    # This intentionally excludes any blank "attached" report that Power BI
    # Desktop creates automatically alongside a .pbip project (those live in
    # the semantic-models source folder and end with ".Report" too, but since
    # we only scan reports_path here they are never picked up).
    reports_copied = []
    for item in reports_path.iterdir():
        if item.is_dir() and item.name.endswith(".Report"):
            dest = Path(staging_dir) / item.name
            shutil.copytree(str(item), str(dest))
            reports_copied.append(item.name)
            print(f"  Staged report: {item.name}")

    # ── Copy parameter.yml to the staging root ────────────────────────────
    # fabric-cicd expects parameter.yml to be in repository_directory (the
    # staging root). file_path entries inside parameter.yml are relative to
    # that same root, so as long as the item folders are in the staging root
    # the existing paths (e.g. "Sales-model.SemanticModel/definition/...")
    # continue to work without any changes.
    if parameter_file_path.exists():
        shutil.copy(str(parameter_file_path), str(Path(staging_dir) / "parameter.yml"))
        print(f"  Staged parameter.yml")

    print(f"Staging complete: {len(models_copied)} semantic model(s), {len(reports_copied)} report(s).")


    # =========================================================================
    # 5. Deploy Semantic Models
    # =========================================================================
    # We deploy Semantic Models before Reports because Reports depend on
    # Semantic Models as their data source. Deploying in dependency order means
    # the Report always binds to an up-to-date model — there is no window where
    # a newly deployed Report is pointing at an outdated Semantic Model.
    #
    # Both deployments use the SAME staging_dir as repository_directory. This
    # is the key change from the single-directory approach: by having both item
    # types in the same directory, the reports workspace can look up the
    # semantic model by its local path when resolving "byPath" references in
    # report definition files — something it could never do when the two item
    # types lived in separate FabricWorkspace instances.
    #
    # item_type_in_scope restricts each call to one item type so that the
    # semantic model step doesn't accidentally try to deploy the report and
    # vice versa.

    print(f"\nDeploying Semantic Models to workspace {workspace_id}...")

    semantic_model_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        token_credential=credential,
        repository_directory=staging_dir,     # ← the combined staging directory
        item_type_in_scope=["SemanticModel"],  # ← only deploy semantic models in this pass
        environment=environment_name,          # ← selects the right parameter.yml values
    )

    publish_all_items(semantic_model_workspace)
    print("Semantic Models deployed successfully.")


    # =========================================================================
    # 6. Deploy Reports
    # =========================================================================
    # Same staging_dir, same workspace instance pattern — just a different
    # item_type_in_scope. Because this workspace instance was initialised with
    # the same staging_dir that contains the semantic model folders, it can
    # find and resolve the "byPath" reference in definition.pbir, look up
    # the semantic model that was just deployed, and automatically rewrite
    # the connection to point at that model's ID in the target workspace.

    print(f"\nDeploying Reports to workspace {workspace_id}...")

    report_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        token_credential=credential,
        repository_directory=staging_dir,  # ← same combined staging directory
        item_type_in_scope=["Report"],     # ← only deploy reports in this pass
        environment=environment_name,
    )

    publish_all_items(report_workspace)
    print("Reports deployed successfully.")

finally:
    # =========================================================================
    # 7. Clean up the staging directory
    # =========================================================================
    # Always remove the temporary staging directory after deployment, whether
    # it succeeded or failed. shutil.rmtree removes the directory and all its
    # contents recursively — the equivalent of "rm -rf" on Linux.
    # The try/finally block guarantees this runs even if an exception is raised
    # above, so we never leave orphaned temp files on the runner.
    shutil.rmtree(staging_dir, ignore_errors=True)
    print("\nStaging directory cleaned up.")

print("Deployment complete.")
