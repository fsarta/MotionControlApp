#!/bin/bash
# ******************************************************************************
# who-moved-current.sh — chi ha cambiato /opt/plcnext/projects/current?
#
# Da usare quando "lo switch non carica il progetto template e resta quello
# utente": serve a distinguere
#   (a) il symlink non e' mai stato cambiato (lo switch si e' fermato prima), da
#   (b) il symlink e' stato cambiato e poi qualcosa l'ha riportato indietro
#       (recovery, reboot con progetto non valido, intervento manuale).
#
# Uso:  sudo ./who-moved-current.sh
# ******************************************************************************

set -u
OUT=/tmp/who-moved.txt
: > "$OUT"
sec() { printf '\n===== %s =====\n' "$*" >> "$OUT"; }
run() { printf '\n$ %s\n' "$*" >> "$OUT"; eval "$@" >> "$OUT" 2>&1; }

sec "1. stato attuale"
run 'readlink -f /opt/plcnext/projects/current'
run 'ls -la /opt/plcnext/projects/ | grep -E "current|AxisControlPanel|PCWE|previous|staging"'
run 'cat /opt/plcnext/user/switch.state 2>/dev/null || echo "switch.state assente"'

sec "2. chi ha toccato il symlink (audit se disponibile)"
run 'command -v auditctl && auditctl -l 2>/dev/null | head'
run 'ausearch -f /opt/plcnext/projects -ts today 2>/dev/null | tail -40 || echo "audit non disponibile"'

sec "3. timeline del boot e del runtime"
run 'uptime'
run 'systemctl show -p ActiveEnterTimestamp -p NRestarts plcnext'
run 'journalctl -u plcnext -b --no-pager | head -80'
run 'journalctl -u plcnext -b --no-pager | grep -iE "project|recover|fallback|safemode|invalid" | tail -40'

sec "4. recovery / fallback della piattaforma"
run 'systemctl status plcnext-recovery --no-pager -l | head -30'
run 'systemctl cat plcnext-recovery 2>/dev/null | head -40'
run 'journalctl -u plcnext-recovery -b --no-pager | tail -40'
run 'ls -la /opt/plcnext/projects/Safemode/ 2>/dev/null'

sec "5. esito del caricamento del progetto"
run 'tail -150 /opt/plcnext/logs/Arp.Init.log'
run 'grep -iE "error|fail|invalid|mismatch|project" /opt/plcnext/logs/Arp.log | tail -60'
run 'cat /opt/plcnext/logs/System.log'

sec "6. file del progetto template (e' completo?)"
run 'ls -la /opt/plcnext/projects/AxisControlPanel/ 2>/dev/null'
run 'ls -la /opt/plcnext/projects/AxisControlPanel/Plc/Eclr/ 2>/dev/null'
run 'ls -la /opt/plcnext/projects/AxisControlPanel/Plc/MC/startup/ 2>/dev/null'
run 'diff -rq /opt/plcnext/projects/PCWE /opt/plcnext/projects/AxisControlPanel 2>/dev/null | head -40'

printf '\nRaccolta completata in %s\n' "$OUT"
echo "Ora esegui:  cat $OUT"
