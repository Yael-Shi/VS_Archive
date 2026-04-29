#!/usr/bin/env bash
set -euo pipefail

# Required env vars:
#   AWS_REGION
#   WORKER_TD
#
# Optional env vars:
#   LIMIT (default: 200)  - how many log messages to fetch
#   STREAMS (default: 50) - how many log streams to scan

: "${AWS_REGION:?Missing AWS_REGION}"
: "${WORKER_TD:?Missing WORKER_TD}"

LIMIT="${LIMIT:-200}"
STREAMS="${STREAMS:-50}"

echo "==> Reading log configuration from task definition: WORKER_TD=$WORKER_TD"

# התיקון כאן: הוספת \" סביב המפתחות עם המקפים
LOG_GROUP="$(
  aws ecs describe-task-definition \
    --region "$AWS_REGION" \
    --task-definition "$WORKER_TD" \
    --query "taskDefinition.containerDefinitions[0].logConfiguration.options.\"awslogs-group\"" \
    --output text
)"

LOG_PREFIX="$(
  aws ecs describe-task-definition \
    --region "$AWS_REGION" \
    --task-definition "$WORKER_TD" \
    --query "taskDefinition.containerDefinitions[0].logConfiguration.options.\"awslogs-stream-prefix\"" \
    --output text
)"

if [[ -z "$LOG_GROUP" || "$LOG_GROUP" == "None" ]]; then
  echo "ERROR: Could not extract LOG_GROUP (awslogs-group) from WORKER_TD=$WORKER_TD"
  exit 1
fi

if [[ -z "$LOG_PREFIX" || "$LOG_PREFIX" == "None" ]]; then
  echo "ERROR: Could not extract LOG_PREFIX (awslogs-stream-prefix) from WORKER_TD=$WORKER_TD"
  exit 1
fi

echo "==> LOG_GROUP  = $LOG_GROUP"
echo "==> LOG_PREFIX = $LOG_PREFIX"
echo

echo "==> Finding newest log stream (with events) in group..."

LOG_STREAM="$(
  aws logs describe-log-streams \
    --region "$AWS_REGION" \
    --log-group-name "$LOG_GROUP" \
    --log-stream-name-prefix "$LOG_PREFIX" \
    --max-items "$STREAMS" \
    --query "logStreams[?lastEventTimestamp!=null] | sort_by(@,&lastEventTimestamp) | [-1].logStreamName" \
    --output text
)"

if [[ -z "$LOG_STREAM" || "$LOG_STREAM" == "None" ]]; then
  echo "ERROR: No log stream with events found under:"
  echo "  LOG_GROUP=$LOG_GROUP"
  echo "  LOG_PREFIX=$LOG_PREFIX"
  echo
  echo "Tip: maybe the task ran but produced no logs yet, or a different prefix is used."
  exit 1
fi

echo "==> LOG_STREAM (newest with events) = $LOG_STREAM"
echo

echo "==> Fetching last $LIMIT log messages..."
echo "------------------------------------------------------------"

aws logs get-log-events \
  --region "$AWS_REGION" \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --start-from-head \
  --limit "$LIMIT" \
  --query "events[].message" \
  --output text

echo "------------------------------------------------------------"
echo "Done."
