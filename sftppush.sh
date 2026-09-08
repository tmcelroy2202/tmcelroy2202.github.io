#!/usr/bin/env bash
# This was written by google gemini, i did not want to use filezilla. it annoyed me. 
set -e

# ================= Configuration =================
REMOTE_USER="username"
REMOTE_HOST="your.server.com"
REMOTE_PORT="22"
REMOTE_PATH="/var/www/html/project"
LOCAL_PATH="."

# Optional: set a dry-run flag by running `./sftppush.sh --dry-run`
DRY_RUN=false
[[ "$1" == "--dry-run" ]] && DRY_RUN=true
# =================================================

echo "==> Pushing $LOCAL_PATH to $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH..."

# Strategy 1: Check if rsync is available on the remote machine
if ssh -p "$REMOTE_PORT" -q "$REMOTE_USER@$REMOTE_HOST" "command -v rsync" >/dev/null 2>&1; then
    echo "==> Using rsync over SSH..."
    
    RSYNC_OPTS=(
        -avz
        --delete
        --filter=':- .gitignore'
        --exclude='.git/'
        -e "ssh -p $REMOTE_PORT"
    )

    if [ "$DRY_RUN" = true ]; then
        RSYNC_OPTS+=(--dry-run)
        echo "[DRY RUN MODE]"
    fi

    rsync "${RSYNC_OPTS[@]}" "$LOCAL_PATH/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/"

# Strategy 2: Fallback to pure SFTP mirror via lftp (if rsync is not on remote)
elif command -v lftp >/dev/null 2>&1; then
    echo "==> Remote lacks rsync. Falling back to LFTP over SFTP..."

    LFTP_CMD="mirror -R -e -p --exclude-glob .git/ $LOCAL_PATH $REMOTE_PATH"
    if [ "$DRY_RUN" = true ]; then
        LFTP_CMD="mirror -R --dry-run --exclude-glob .git/ $LOCAL_PATH $REMOTE_PATH"
        echo "[DRY RUN MODE]"
    fi

    lftp -u "$REMOTE_USER", "sftp://$REMOTE_HOST:$REMOTE_PORT" <<EOF
set sftp:auto-confirm yes
$LFTP_CMD
quit
EOF

else
    echo "Error: Neither remote rsync nor local lftp are available."
    echo "Install lftp locally ('brew install lftp' or 'apt install lftp') for pure SFTP fallback."
    exit 1
fi

echo "==> Push complete."
