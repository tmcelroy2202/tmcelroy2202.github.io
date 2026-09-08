#!/usr/bin/env bash
# IMPORTANT: this was written by google gemini, i just didnt want to use filezilla. i am disclosing that this was written by google gemini. this is not helping with the material of the class, and so im assuming is fine, especially with ai policy expressed thus far in the class. 
#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# 1. Source .env
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "Error: .env file not found at $ENV_FILE"
    echo "Copy .env.example to .env and configure your credentials."
    exit 1
fi

# 2. Validate Required Variables
LOCAL_PATH="${LOCAL_PATH:-.}"
REMOTE_PORT="${REMOTE_PORT:-22}"
DELETE_REMOVED="${DELETE_REMOVED:-true}"

for var in REMOTE_HOST REMOTE_USER REMOTE_PATH; do
    if [[ -z "${!var}" ]]; then
        echo "Error: Required variable '$var' is missing from .env"
        exit 1
    fi
done

# Resolve absolute local path to avoid directory ambiguity
RESOLVED_LOCAL_PATH="$(cd "$LOCAL_PATH" && pwd)"
SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"

DRY_RUN=false
[[ "$1" == "--dry-run" ]] && DRY_RUN=true

echo "==> Preparing to push '$RESOLVED_LOCAL_PATH' to $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH..."

# 3. Determine SSH Authentication
SSH_CMD="ssh -p $REMOTE_PORT"
if [[ -n "$SSH_KEY_PATH" && -f "$SSH_KEY_PATH" ]]; then
    SSH_CMD="$SSH_CMD -i $SSH_KEY_PATH"
fi

USE_SSHPASS=false
if [[ -n "$REMOTE_PASSWORD" ]]; then
    if command -v sshpass >/dev/null 2>&1; then
        USE_SSHPASS=true
    else
        echo "Warning: REMOTE_PASSWORD is set, but 'sshpass' is not installed."
    fi
fi

# 4. Strategy A: rsync over SSH
REMOTE_CHECK_CMD="$SSH_CMD -q $REMOTE_USER@$REMOTE_HOST 'command -v rsync'"
if [ "$USE_SSHPASS" = true ]; then
    REMOTE_CHECK_CMD="sshpass -p '$REMOTE_PASSWORD' $REMOTE_CHECK_CMD"
fi

if eval "$REMOTE_CHECK_CMD" >/dev/null 2>&1; then
    echo "==> Using rsync (respecting .gitignore)..."

    RSYNC_FLAGS=(
        -avz
        --filter=':- .gitignore'
        --filter=':- .git/info/exclude'
        --exclude='.git/'
        --exclude='.env*'
    )

    [[ "$DELETE_REMOVED" == "true" ]] && RSYNC_FLAGS+=(--delete)
    [[ "$DRY_RUN" == "true" ]] && RSYNC_FLAGS+=(--dry-run)

    SSH_TUNNEL="$SSH_CMD"
    if [ "$USE_SSHPASS" = true ]; then
        SSH_TUNNEL="sshpass -p '$REMOTE_PASSWORD' $SSH_CMD"
    fi

    if [ "$DRY_RUN" = true ]; then echo "[DRY RUN MODE]"; fi

    rsync "${RSYNC_FLAGS[@]}" -e "$SSH_TUNNEL" "$RESOLVED_LOCAL_PATH/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/"

# 5. Strategy B: Pure SFTP via lftp
elif command -v lftp >/dev/null 2>&1; then
    echo "==> Remote lacks rsync. Using lftp mirror over SFTP..."

    # Build a clean exclusion list file for lftp
    EXCLUDE_FILE="$(mktemp)"
    trap 'rm -f "$EXCLUDE_FILE"' EXIT

    # Core files to exclude
    cat <<'EOF' > "$EXCLUDE_FILE"
.git*
.git/**
.env*
sftppush.sh
EOF

    # Append parsed patterns from .gitignore
    if [[ -f "$RESOLVED_LOCAL_PATH/.gitignore" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            line="$(echo "$line" | sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//' -e $'s/\r$//')"
            [[ -z "$line" || "$line" =~ ^# ]] && continue
            
            pattern="${line#/}"
            pattern="${pattern%/}"
            
            echo "$pattern" >> "$EXCLUDE_FILE"
            echo "$pattern/**" >> "$EXCLUDE_FILE"
        done < "$RESOLVED_LOCAL_PATH/.gitignore"
    fi

    # Mirror flags: -R (reverse/upload)
    MIRROR_ARGS="-R --exclude-glob-from=$EXCLUDE_FILE"
    [[ "$DELETE_REMOVED" == "true" ]] && MIRROR_ARGS="$MIRROR_ARGS -e"
    [[ "$DRY_RUN" == "true" ]] && MIRROR_ARGS="$MIRROR_ARGS --dry-run"

    AUTH_CRED="$REMOTE_USER"
    if [[ -n "$REMOTE_PASSWORD" ]]; then
        AUTH_CRED="$REMOTE_USER:$REMOTE_PASSWORD"
    fi

    CONNECT_LINE=""
    if [[ -n "$SSH_KEY_PATH" && -f "$SSH_KEY_PATH" ]]; then
        CONNECT_LINE="set sftp:connect-program 'ssh -a -x -i $SSH_KEY_PATH';"
    fi

    if [ "$DRY_RUN" = true ]; then echo "[DRY RUN MODE]"; fi

    # Pipe commands directly via stdin to prevent shell-quote mangling
    lftp -u "$AUTH_CRED" -p "$REMOTE_PORT" "sftp://$REMOTE_HOST" <<EOF
set sftp:auto-confirm yes
$CONNECT_LINE
mirror $MIRROR_ARGS $RESOLVED_LOCAL_PATH $REMOTE_PATH
quit
EOF

else
    echo "Error: Neither remote rsync nor local lftp is available."
    exit 1
fi

echo "==> Sync complete!"
