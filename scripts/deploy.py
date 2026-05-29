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
# =============================================================================

import os
import sys
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
    print("No parameter file found — deploying Semantic Models without parameter substitution.")


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
# 4. Deploy Semantic Models
# =============================================================================
# We deploy Semantic Models before Reports because Reports depend on Semantic
# Models as their data source. Deploying in dependency order means the Report
# always binds to an up-to-date model — there is no window where a newly
# deployed Report is pointing at an outdated Semantic Model.
#
# Key FabricWorkspace arguments:
#   workspace_id        — the Fabric workspace to deploy into
#   token_credential    — the Azure credential object from step 3
#   repository_directory— where fabric-cicd looks for item definition files.
#                         This points at the content repo's semantic-models
#                         folder, which also contains parameter.yml.
#   item_type_in_scope  — restricts deployment to only Semantic Models, so we
#                         don't accidentally publish other item types that might
#                         be in the folder
#   environment         — the environment name (dev / test / prod). fabric-cicd
#                         uses this to select the correct replacement values
#                         from parameter.yml (e.g. the "prod" server name)

print(f"Deploying Semantic Models to workspace {workspace_id}...")

semantic_model_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    token_credential=credential,
    repository_directory=str(semantic_models_path),
    item_type_in_scope=["SemanticModel"],
    environment=environment_name,
)

# publish_all_items scans repository_directory for item definitions and creates
# or updates them in the target workspace. Items already in the workspace are
# updated in place; new items are created. Items that exist in the workspace
# but are absent from the folder are left untouched — nothing is deleted.
publish_all_items(semantic_model_workspace)

print("Semantic Models deployed successfully.")


# =============================================================================
# 5. Deploy Reports
# =============================================================================
# Same pattern as above, now targeting the reports folder and restricting to
# the Report item type. Running this after the Semantic Model step ensures the
# data source the report connects to is already up to date.
#
# Reports don't use a parameter file in this pipeline — their environment-
# specific behaviour comes from binding to the correct Semantic Model, which
# was already parameterised in the step above.

print(f"Deploying Reports to workspace {workspace_id}...")

report_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    token_credential=credential,
    repository_directory=str(reports_path),
    item_type_in_scope=["Report"],
    environment=environment_name,
)

publish_all_items(report_workspace)

print("Reports deployed successfully.")
print("Deployment complete.")
