# =============================================================================
# deploy.py
#
# This script is called by the GitHub Actions workflow to deploy Power BI
# Reports and Semantic Models to a Fabric workspace.
#
# It uses the `fabric-cicd` Python package, which is Microsoft's open-source
# library for automating Fabric deployments. Under the hood it calls the
# Fabric REST API on your behalf, handling authentication and item publishing.
#
# The script reads all configuration from environment variables that are
# injected by the GitHub Actions workflow — it does not contain any hardcoded
# credentials or workspace IDs. This makes it safe to commit to source control.
# =============================================================================

import os
import sys
from pathlib import Path

# fabric_cicd is the Microsoft package that handles communication with the
# Fabric REST API. FabricWorkspace represents the target workspace and
# publish_all_items deploys everything in scope to it.
from fabric_cicd import FabricWorkspace, publish_all_items

# azure-identity provides authentication building blocks. ClientSecretCredential
# implements the OAuth 2.0 "client credentials" flow, which is the standard way
# for automated pipelines to authenticate without a human signing in.
from azure.identity import ClientSecretCredential


# =============================================================================
# 1. Read configuration from environment variables
# =============================================================================
# os.environ["KEY"] reads a value that was set in the environment before this
# script was launched. The GitHub Actions workflow sets these values from
# GitHub Secrets and Variables — they are never written into this file.
#
# ENVIRONMENT_NAME is the Git branch name (dev / test / prod). It is used in
# two ways:
#   a) To select the right replacement value in the parameter file
#      (e.g. the dev connection string vs the prod connection string).
#   b) For log output so it's clear which environment was targeted.
#
# If a required variable is missing we print a clear error and exit immediately.
# sys.exit(1) signals failure to GitHub Actions, which marks the run as failed.
# "Fail fast" is better than letting the script crash later with a confusing
# error deep inside a library call.

required_vars = [
    "AZURE_CLIENT_ID",       # The service principal's unique application ID
    "AZURE_CLIENT_SECRET",   # The service principal's password / secret key
    "AZURE_TENANT_ID",       # Your organisation's Azure Active Directory tenant ID
    "FABRIC_WORKSPACE_ID",   # The GUID of the Fabric workspace we're deploying into
    "ENVIRONMENT_NAME",      # The branch / environment name: dev, test, or prod
]

missing = [var for var in required_vars if not os.environ.get(var)]
if missing:
    print(f"ERROR: The following required environment variables are not set: {missing}")
    print("These should be configured as GitHub Secrets / Variables in your repository settings.")
    sys.exit(1)

client_id        = os.environ["AZURE_CLIENT_ID"]
client_secret    = os.environ["AZURE_CLIENT_SECRET"]
tenant_id        = os.environ["AZURE_TENANT_ID"]
workspace_id     = os.environ["FABRIC_WORKSPACE_ID"]
environment_name = os.environ["ENVIRONMENT_NAME"]   # e.g. "dev", "test", "prod"


# =============================================================================
# 2. Locate the artifacts folders and parameter file
# =============================================================================
# Path(__file__) is the path to this script file itself.
# .parent gives us the folder containing it (scripts/).
# .parent again gives us the repo root.
# We then build paths to the sub-folders relative to the repo root.

repo_root = Path(__file__).parent.parent

reports_path         = repo_root / "artifacts" / "reports"
semantic_models_path = repo_root / "artifacts" / "semantic-models"

# The parameter file holds environment-specific values that should be swapped
# into your Semantic Model definitions at deploy time — for example, replacing
# a dev database server name with the prod one. See the comments in
# artifacts/semantic-models/parameter.yml for the full format.
#
# By convention, fabric-cicd looks for a file named parameter.yml inside the
# repository_directory of the workspace (i.e. the artifacts folder). Placing
# it at artifacts/semantic-models/parameter.yml follows that convention and
# keeps the parameter file alongside the items it configures.
#
# If the file doesn't exist the deployment still runs — parameters are optional.
# fabric-cicd will simply skip parameterization and deploy the files as-is.
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
# this credential object whenever it needs to make an API request.

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
# Models as their data source. If we deployed Reports first and the Semantic
# Model definition had changed, the Report could briefly point at a stale schema.
# Deploying in dependency order avoids that inconsistency window.
#
# Key FabricWorkspace arguments:
#   workspace_id        — the Fabric workspace to deploy into
#   token_credential    — the Azure credential object from step 3
#   repository_directory— where fabric-cicd looks for item definition files
#   item_type_in_scope  — restricts deployment to only Semantic Models so we
#                         don't accidentally publish other item types
#   environment         — the environment name (dev / test / prod). fabric-cicd
#                         uses this to select the correct replacement values from
#                         the parameter file (e.g. the "dev" server name vs "prod")

print(f"Deploying Semantic Models to workspace {workspace_id}...")

semantic_model_workspace = FabricWorkspace(
    workspace_id=workspace_id,
    token_credential=credential,
    repository_directory=str(semantic_models_path),
    item_type_in_scope=["SemanticModel"],
    environment=environment_name,
)

# publish_all_items scans the repository_directory for item definitions and
# creates or updates them in the target workspace. Items already in the workspace
# are updated in place; items not yet in the workspace are created. Items that
# exist in the workspace but are not in the folder are left untouched — this is
# not a destructive sync, so nothing gets deleted automatically.
publish_all_items(semantic_model_workspace)

print("Semantic Models deployed successfully.")


# =============================================================================
# 5. Deploy Reports
# =============================================================================
# Same pattern as above, now targeting the reports folder and restricting to
# the Report item type. Running this after the Semantic Model step ensures the
# data source the report connects to is already up to date.
#
# Reports don't use a parameter file in this pipeline — their environment-specific
# behaviour comes from binding to the correct Semantic Model, which was already
# parameterised in the step above.

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
