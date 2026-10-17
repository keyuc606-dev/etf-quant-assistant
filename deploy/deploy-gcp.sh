#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT_ID:?}" "${PROJECT_NUMBER:?}" "${GITHUB_SHA:?}"
REGION=asia-northeast1
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/etf/assistant:$GITHUB_SHA"
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
docker build -t "$IMAGE" .
docker push "$IMAGE"
for NAME in ACCOUNT_STATE_TOKEN ACCOUNT_STATE_REPO TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  test -n "${!NAME}" || { echo "Missing $NAME"; exit 1; }
  printf '%s' "${!NAME}" | gcloud secrets versions add "$NAME" --data-file=- >/dev/null
done
for SESSION in morning closing; do
  JOB="etf-$SESSION"
  gcloud run jobs deploy "$JOB" --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE" --service-account="etf-runtime@$PROJECT_ID.iam.gserviceaccount.com" --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=1200s --cpu=1 --memory=1Gi --args="$SESSION" --set-env-vars="GITHUB_SHA=$GITHUB_SHA" --set-secrets='ACCOUNT_STATE_TOKEN=ACCOUNT_STATE_TOKEN:latest,ACCOUNT_STATE_REPO=ACCOUNT_STATE_REPO:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest' --quiet
  gcloud run jobs add-iam-policy-binding "$JOB" --region="$REGION" --member="serviceAccount:etf-scheduler@$PROJECT_ID.iam.gserviceaccount.com" --role=roles/run.invoker >/dev/null
done
# Verify state access before enabling any schedule. No messages or state writes.
gcloud run jobs execute etf-morning --region="$REGION" --args=morning,--check --wait
for SESSION in morning closing; do
  JOB="etf-$SESSION"
  if [ "$SESSION" = morning ]; then CRON='30,35,40 10 * * 1-5'; else CRON='0,5,10 14 * * 1-5'; fi
  METHOD=create
  if gcloud scheduler jobs describe "$JOB" --location="$REGION" >/dev/null 2>&1; then METHOD=update; fi
  gcloud scheduler jobs "$METHOD" http "$JOB" --location="$REGION" --schedule="$CRON" --time-zone=Asia/Shanghai --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/$JOB:run" --http-method=POST --oauth-service-account-email="etf-scheduler@$PROJECT_ID.iam.gserviceaccount.com" --headers=Content-Type=application/json --message-body='{}' --max-retry-attempts=0 --attempt-deadline=180s --quiet
done
echo 'Deployment and read-only preflight passed. Check the next trading-day delivery receipts.'
