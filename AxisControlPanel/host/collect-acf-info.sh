#!/bin/bash
# ******************************************************************************
# collect-acf-info.sh — dove viene caricata la configurazione ACF su QUESTO firmware
#
# Perche' serve:
#   il processo principale gira con
#     --config=/etc/plcnext/device/Default.acf.config
#   cioe' la configurazione ACF di SISTEMA sta in /etc/plcnext/device/, non in
#   /opt/plcnext/projects/Default/. Per questo il file
#   /opt/plcnext/projects/Default/TriggerRetain.acf.config non e' mai stato
#   caricato (componente mai istanziato -> backups/ vuota, "restore" inerte).
#
#   Questo script raccoglie in un colpo solo tutto cio' che serve per decidere
#   DOVE registrare il componente in modo deterministico.
#
# Uso:  sudo /tmp/collect-acf-info.sh
# ******************************************************************************

set -u
OUT=/tmp/acf-info.txt
: > "$OUT"

sec() { printf '\n===== %s =====\n' "$*" >> "$OUT"; }
run() { printf '\n$ %s\n' "$*" >> "$OUT"; eval "$@" >> "$OUT" 2>&1; }

sec "1. configurazione ACF di sistema"
run 'cat /etc/plcnext/device/Default.acf.config'
run 'ls -la /etc/plcnext/device/'

sec "2. settings del device (percorsi inclusi)"
run 'cat /etc/plcnext/Device.acf.settings'

sec "3. configurazione ACF dentro /opt/plcnext/projects/Default"
run 'ls -la /opt/plcnext/projects/Default/'
run 'cat /opt/plcnext/projects/Default/Default.acf.config'
run 'cat /opt/plcnext/projects/Default/PLCnextBase.acf.config'

sec "4. acf.config alla RADICE dei progetti (include \$ARP_PATH_PROJECT_CURRENT_DIR\$/*.acf.config)"
run 'for d in /opt/plcnext/projects/*/; do echo "--- $d"; ls -la "$d"*.acf.config 2>/dev/null; done'

sec "5. dove compare TriggerRetain"
run 'grep -rl "TriggerRetain" /etc/plcnext /opt/plcnext/projects 2>/dev/null'

sec "6. progetto attivo"
run 'readlink -f /opt/plcnext/projects/current'
run 'ls -la /opt/plcnext/projects/TriggerRetain/ 2>/dev/null || echo "cartella TriggerRetain ASSENTE"'
run 'cat /opt/plcnext/user/BackupAction.txt 2>/dev/null || echo "BackupAction.txt assente"'

sec "7. log del runtime"
# i log utili sono in /opt/plcnext/logs/ (Arp.log), NON solo in journald
run 'ls -la /opt/plcnext/logs/ 2>/dev/null | head -20'
run 'tail -200 /opt/plcnext/logs/Arp.log | grep -iE "acf|component|library|TriggerRetain|retain"'
run 'cat /opt/plcnext/logs/Custom.log'
run 'journalctl -u plcnext -b --no-pager | grep -iE "triggerretain|retain" | tail -40'

sec "8. servizio runtime"
run 'systemctl is-active plcnext'
run 'systemctl cat plcnext'

printf '\nRaccolta completata in %s\n' "$OUT"
echo "Ora esegui:  cat $OUT"
