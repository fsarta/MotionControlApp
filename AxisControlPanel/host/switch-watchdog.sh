#!/bin/bash
# ******************************************************************************
# switch-watchdog.sh — rete di sicurezza per il cambio progetto di AxisControlPanel
#
# Perche' serve:
#   il cambio di progetto riavvia il controller, quindi il container Python muore
#   a meta' sequenza. Se il firmware non arriva a "Running" (tipico: WBM 502, solo
#   LED RDY lampeggiante) nessuno se ne accorge e in campo si finisce col reset di
#   fabbrica. Questo script gira SULL'HOST e chiude il cerchio.
#
# Cosa fa, ad ogni esecuzione:
#   1. legge /opt/plcnext/user/switch.state lasciato dall'app
#   2. se lo switch e' "pending" e la deadline e' passata:
#        a. prova a riavviare il solo runtime PLCnext (systemd: plcnext)
#        b. se ancora ko -> reboot del sistema
#   3. marca lo stato "done" appena il firmware e' veramente su
#
# Installazione (sul controller, come admin):
#   sudo cp switch-watchdog.sh /opt/plcnext/user/switch-watchdog.sh
#   sudo chmod +x /opt/plcnext/user/switch-watchdog.sh
#   ( crontab -l 2>/dev/null | grep -v switch-watchdog ; \
#     echo '@reboot sleep 60 && /opt/plcnext/user/switch-watchdog.sh' ; \
#     echo '* * * * * /opt/plcnext/user/switch-watchdog.sh' ) | crontab -
# ******************************************************************************

set -u

STATE="/opt/plcnext/user/switch.state"
LOG="/opt/plcnext/logs/switch-watchdog.log"
LOCK="/tmp/switch-watchdog.lock"
APP_PROJECT="AxisControlPanel"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$LOG"
}

[ -f "$STATE" ] || exit 0

# un solo watchdog alla volta
if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

json_get() {   # json_get <chiave> -> valore (stringhe e numeri)
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",]*\)\"\{0,1\}.*/\1/p" "$STATE" | head -1
}

PHASE=$(json_get phase)
TARGET=$(json_get target_project)
STEP=$(json_get step)
DEADLINE=$(json_get deadline)
NOW=$(date +%s)

case "$PHASE" in
    done|failed|idle|"") exit 0 ;;
    pending) ;;
    *) log "fase sconosciuta '$PHASE', ignoro"; exit 0 ;;
esac

# il firmware e' su? (WBM/backend raggiungibile oppure processo runtime presente)
http_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl -sf -m 5 -o /dev/null http://127.0.0.1/wbm/ 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 5 -O /dev/null http://127.0.0.1/wbm/ 2>/dev/null
    else
        return 1
    fi
}

plc_running() {
    # 1) backend raggiungibile = firmware pronto (il 502 significa "non pronto")
    if http_ok; then
        return 0
    fi
    # 2) processo runtime presente
    if pgrep -f 'Arp.System.Application' >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# comando di restart del runtime: systemd su FW >= 2022.0, init.d sui piu' vecchi
runtime_restart() {
    if [ -x /usr/bin/systemctl ] && systemctl list-unit-files 2>/dev/null | grep -q '^plcnext\.service'; then
        systemctl restart plcnext
    elif [ -x /etc/init.d/plcnext ]; then
        /etc/init.d/plcnext restart
    elif [ -x /usr/bin/systemctl ]; then
        systemctl restart plcnext
    else
        return 127
    fi
}

if plc_running; then
    log "firmware OK, switch verso '$TARGET' completato (step era '$STEP')"
    sed -i 's/"phase"[[:space:]]*:[[:space:]]*"pending"/"phase": "done"/' "$STATE" || true
    exit 0
fi

if [ -n "$DEADLINE" ] && [ "$NOW" -lt "$DEADLINE" ] 2>/dev/null; then
    exit 0   # ancora dentro la finestra di boot: non intervenire
fi

log "BOOT NON COMPLETATO per '$TARGET' (step '$STEP', deadline $DEADLINE): tentativo 1 = restart runtime"
runtime_restart >>"$LOG" 2>&1

sleep 45

if plc_running; then
    log "recuperato con restart del runtime PLCnext"
    sed -i 's/"phase"[[:space:]]*:[[:space:]]*"pending"/"phase": "done"/' "$STATE" || true
    exit 0
fi

log "restart firmware insufficiente: tentativo 2 = reboot del sistema"
sync
/sbin/reboot -f >>"$LOG" 2>&1
exit 0
