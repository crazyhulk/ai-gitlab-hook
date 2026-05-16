#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/run.pid"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"
LOG_DIR="$SCRIPT_DIR/logs"
NOHUP_LOG="$LOG_DIR/nohup.out"

# Parse port: env var > config.yaml > default 8080
if [[ -z "${PORT:-}" ]]; then
    PORT=$(grep -E '^\s+port:' "$CONFIG_FILE" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"')
    PORT=${PORT:-8080}
fi

PYTHON=$(command -v python3)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
_step()  { echo -e "${BLUE}[....] ${NC} $*"; }

# PID currently holding the port (empty if none)
_port_pid() {
    lsof -ti tcp:"$PORT" 2>/dev/null | head -1 || true
}

# Check if a PID is alive
_pid_alive() {
    kill -0 "$1" 2>/dev/null
}

# Read our PID from file; empty string if missing or invalid
_read_pid() {
    [[ -f "$PID_FILE" ]] || return 0
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')
    [[ "$pid" =~ ^[0-9]+$ ]] && echo "$pid" || true
}

# ── status ────────────────────────────────────────────────────────────────────

do_status() {
    local pid port_pid
    pid=$(_read_pid)
    port_pid=$(_port_pid)

    echo "─────────────────────────────────"
    echo "  Service : ai-gitlab-hook"
    echo "  Port    : $PORT"
    echo "─────────────────────────────────"

    if [[ -n "$pid" ]] && _pid_alive "$pid"; then
        echo -e "  Process : ${GREEN}running${NC}  (pid=$pid)"
    elif [[ -n "$pid" ]]; then
        echo -e "  Process : ${RED}stale PID${NC} (pid=$pid in run.pid, process is gone)"
    else
        echo -e "  Process : ${RED}not running${NC}"
    fi

    if [[ -n "$port_pid" ]]; then
        echo -e "  Port    : ${GREEN}listening${NC} (pid=$port_pid)"
    else
        echo -e "  Port    : ${RED}not listening${NC}"
    fi
    echo "─────────────────────────────────"
}

# ── deps ──────────────────────────────────────────────────────────────────────

_ensure_deps() {
    local req="$SCRIPT_DIR/requirements.txt"
    [[ -f "$req" ]] || return 0
    _step "Checking dependencies..."
    if "$PYTHON" -m pip install -q -r "$req"; then
        _info "Dependencies OK"
    else
        _error "Failed to install dependencies. Check: pip install -r $req"
        return 1
    fi
}

# ── start ─────────────────────────────────────────────────────────────────────

do_start() {
    local pid port_pid
    pid=$(_read_pid)

    # Already running via our PID file?
    if [[ -n "$pid" ]] && _pid_alive "$pid"; then
        _warn "Already running (pid=$pid). Use 'restart' or 'stop' first."
        return 1
    fi

    # Port held by a foreign process?
    port_pid=$(_port_pid)
    if [[ -n "$port_pid" ]]; then
        _error "Port $PORT is already occupied by pid=$port_pid (not our process)."
        _error "Free it first:  kill $port_pid"
        return 1
    fi

    # Clean up stale PID file
    [[ -n "$pid" ]] && rm -f "$PID_FILE" && _warn "Removed stale run.pid (pid=$pid was dead)"

    _ensure_deps || return 1

    mkdir -p "$LOG_DIR"
    _step "Starting service (port=$PORT)..."

    nohup "$PYTHON" -m uvicorn app.main:app \
        --host 0.0.0.0 --port "$PORT" \
        --timeout-graceful-shutdown 8 \
        >> "$NOHUP_LOG" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # Wait for the port to be listening (max 15 s)
    local i=0
    while [[ $i -lt 30 ]]; do
        if [[ -n "$(_port_pid)" ]]; then
            _info "Service is up  (pid=$new_pid, port=$PORT)"
            _info "Logs: $LOG_DIR/"
            return 0
        fi
        # Process died early?
        if ! _pid_alive "$new_pid"; then
            _error "Process died during startup. Check: $NOHUP_LOG"
            rm -f "$PID_FILE"
            return 1
        fi
        sleep 0.5
        i=$((i + 1))
    done

    _warn "Process is alive (pid=$new_pid) but port $PORT not yet listening after 15s."
    _warn "Check: $NOHUP_LOG"
}

# ── stop ──────────────────────────────────────────────────────────────────────

do_stop() {
    local pid port_pid
    pid=$(_read_pid)
    port_pid=$(_port_pid)

    # Nothing to stop
    if [[ -z "$pid" ]] && [[ -z "$port_pid" ]]; then
        _warn "Service is not running."
        return 0
    fi

    # Mismatch: PID file points to one process, port is held by another
    if [[ -n "$pid" ]] && [[ -n "$port_pid" ]] && [[ "$pid" != "$port_pid" ]]; then
        _warn "PID mismatch: run.pid=$pid, port owner=$port_pid — stopping both."
        kill -TERM "$port_pid" 2>/dev/null || true
    fi

    local target="${pid:-$port_pid}"
    _step "Stopping pid=$target (SIGTERM)..."
    kill -TERM "$target" 2>/dev/null || true

    # Graceful shutdown window: 10 s
    local i=0
    while [[ $i -lt 20 ]]; do
        _pid_alive "$target" || break
        sleep 0.5
        i=$((i + 1))
    done

    # Force kill if still alive
    if _pid_alive "$target"; then
        _warn "Graceful shutdown timed out — sending SIGKILL to pid=$target"
        kill -KILL "$target" 2>/dev/null || true
        sleep 0.5
    fi

    if _pid_alive "$target"; then
        _error "Failed to stop pid=$target"
        return 1
    fi

    rm -f "$PID_FILE"

    # Final port check
    local leftover
    leftover=$(_port_pid)
    if [[ -n "$leftover" ]]; then
        _warn "Process stopped but port $PORT still held by pid=$leftover"
    else
        _info "Service stopped. Port $PORT is free."
    fi
}

# ── restart ───────────────────────────────────────────────────────────────────

do_restart() {
    do_stop || true
    sleep 0.5
    do_start
}

# ── main ──────────────────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
}

cd "$SCRIPT_DIR"

case "${1:-}" in
    start)   do_start   ;;
    stop)    do_stop    ;;
    restart) do_restart ;;
    status)  do_status  ;;
    *)       usage      ;;
esac
