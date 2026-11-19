#!/bin/sh
cd "$(dirname "$0")"
PIDFILE="./app.pid"

# When the WBM stops the app, run this script with the argument "stop"
if [ "$1" = "stop" ]; then
    echo "Stopping AxisControlPanel..."
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        # Manually send SIGTERM to the python process!
        kill -15 $PID
        rm -f "$PIDFILE"
    else
        pkill -15 -f "main.py" || true
    fi
    exit 0
fi

# Normal start (argument "start" or no argument)
chmod +x main.py
python3 -u main.py &
echo $! > "$PIDFILE"
wait $!