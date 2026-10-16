#!/usr/bin/env bash
# Run in Google Cloud Shell after selecting a billing-enabled project.
# Contains no user secrets. Idempotent: safe to rerun.
set -euo pipefail
PROJECT="${1:?Usage: bash deploy/bootstrap-gcp.sh PROJECT_ID}"
REGION=asia-northeast1
gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com iamcredentials.googleapis.com sts.googleapis.com
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for NAME in etf-deployer etf-runtime etf-scheduler; do
  gcloud iam service-accounts describe "$NAME@$PROJECT.iam.gserviceaccount.com" >/dev/null 2>&1 || gcloud iam service-accounts create "$NAME"
done
gcloud artifacts repositories describe etf --location="$REGION" >/dev/null 2>&1 || gcloud artifacts repositories create etf --repository-format=docker --location="$REGION"
gcloud iam workload-identity-pools describe github --location=global >/dev/null 2>&1 || gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers describe etf --location=global --workload-identity-pool=github >/dev/null 2>&1 || gcloud iam workload-identity-pools providers create-oidc etf --location=global --workload-identity-pool=github --issuer-uri=https://token.actions.githubusercontent.com --attribute-mapping='google.subject=assertion.sub,attribute.repository=assertion.repository' --attribute-condition="assertion.repository == 'keyuc606-dev/etf-quant-assistant' && assertion.ref == 'refs/heads/main'"
gcloud iam service-accounts add-iam-policy-binding "etf-deployer@$PROJECT.iam.gserviceaccount.com" --role=roles/iam.workloadIdentityUser --member="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/keyuc606-dev/etf-quant-assistant"
for ROLE in run.admin cloudscheduler.admin artifactregistry.writer secretmanager.admin serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:etf-deployer@$PROJECT.iam.gserviceaccount.com" --role="roles/$ROLE" --condition=None >/dev/null
done
for NAME in etf-runtime etf-scheduler; do
  gcloud iam service-accounts add-iam-policy-binding "$NAME@$PROJECT.iam.gserviceaccount.com" --member="serviceAccount:etf-deployer@$PROJECT.iam.gserviceaccount.com" --role=roles/iam.serviceAccountUser >/dev/null
done
for NAME in ACCOUNT_STATE_TOKEN ACCOUNT_STATE_REPO TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  gcloud secrets describe "$NAME" >/dev/null 2>&1 || gcloud secrets create "$NAME" --replication-policy=automatic
  gcloud secrets add-iam-policy-binding "$NAME" --member="serviceAccount:etf-runtime@$PROJECT.iam.gserviceaccount.com" --role=roles/secretmanager.secretAccessor >/dev/null
done
printf '\nBootstrap ready. GitHub Deploy production scheduler inputs: project_id=%s project_number=%s\n' "$PROJECT" "$NUMBER"
printf 'No secret values needed. Configure a small billing budget alert in the Cloud console.\n'
