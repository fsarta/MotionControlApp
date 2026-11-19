#!/bin/bash
# ******************************************************************************
# install-retain-component.sh — registra il componente TriggerRetain SULL'HOST
#
# Perche' esiste: cosi' si puo' provare il componente ACF da solo (senza app e
# senza rebuilderne l'immagine), che e' il modo piu' rapido per verificare che
# la registrazione sia corretta.
#
#   sudo ./install-retain-component.sh /tmp/TriggerRetain
#     /tmp/TriggerRetain deve contenere: lib/libTriggerRetain.so
#
# Crea:
#   /opt/plcnext/projects/TriggerRetain/lib/libTriggerRetain.so
#   /opt/plcnext/projects/TriggerRetain/TriggerRetainLibrary.acf.config
#   /opt/plcnext/projects/Default/TriggerRetain.acf.config
# e poi riavvia il runtime per verificare il caricamento.
# ******************************************************************************
set -eu

SRC="${1:-/tmp/TriggerRetain}"
P=/opt/plcnext/projects/TriggerRetain
D=/opt/plcnext/projects/Default
LIB="$SRC/lib/libTriggerRetain.so"

[ "$(id -u)" = "0" ] || { echo "esegui con sudo"; exit 1; }
[ -f "$LIB" ] || { echo "manca $LIB"; exit 1; }
[ -d "$D" ] || { echo "manca $D"; exit 1; }

mkdir -p "$P/lib"
cp "$LIB" "$P/lib/libTriggerRetain.so"
chown -R 1002:1002 "$P"

cat > "$P/TriggerRetainLibrary.acf.config" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<AcfConfigurationDocument
  xmlns="http://www.phoenixcontact.com/schema/acfconfig"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.phoenixcontact.com/schema/acfconfig.xsd"
  schemaVersion="1.0" >

  <Libraries>
    <Library name="TriggerRetain.TriggerRetainLibrary"
             binaryPath="$ARP_PROJECTS_DIR$/TriggerRetain/lib/libTriggerRetain.so" />
  </Libraries>

  <Components>
    <Component name="TriggerRetainComponent1"
               type="TriggerRetain.TriggerRetainComponent"
               library="TriggerRetain.TriggerRetainLibrary" />
  </Components>

</AcfConfigurationDocument>
EOF

cat > "$D/TriggerRetain.acf.config" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<AcfConfigurationDocument
  xmlns="http://www.phoenixcontact.com/schema/acfconfig"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.phoenixcontact.com/schema/acfconfig.xsd"
  schemaVersion="1.0" >
  <Includes>
    <Include path="$ARP_PROJECTS_DIR$/TriggerRetain/*.acf.config" />
  </Includes>
</AcfConfigurationDocument>
EOF

chown 1002:1002 "$D/TriggerRetain.acf.config"
chmod 644 "$D/TriggerRetain.acf.config"
sync

echo "--- installato:"
ls -la "$P" "$P/lib" "$D"/TriggerRetain.acf.config

echo "--- riavvio runtime..."
systemctl restart plcnext
sleep 45

echo "--- stato runtime:"
systemctl is-active plcnext

echo "--- tracce del componente nei log:"
grep -i -E "triggerretain|TriggerRetainComponent" /opt/plcnext/logs/*.log 2>/dev/null | tail -20 || true
grep -i -E "retain" /opt/plcnext/logs/Arp.log 2>/dev/null | tail -20 || true
echo "--- backup retain:"
ls -la /opt/plcnext/retaining/backups/ || true
