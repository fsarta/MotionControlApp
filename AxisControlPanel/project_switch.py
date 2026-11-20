# -*- coding: utf-8 -*-
"""
project_switch.py — cambio progetto PLCnext utente <-> template, robusto.

Perche' esiste questo modulo
----------------------------
Il cambio di progetto (/opt/plcnext/projects/current) deve concludersi con un
riavvio di SISTEMA reale. `IDeviceControlService.RestartDevice()` (gRPC) e' il
"restart of the firmware requested by an app" che Phoenix Contact documenta come
sporadicamente non funzionante: in quel caso il firmware non arriva a ready e il
WBM risponde "502 Bad Gateway" con il solo LED RDY lampeggiante.
Con un reboot reale lo stesso identico progetto parte correttamente.

Regole implementate qui:
  1. si scrivono i file, si fa `sync`, si VERIFICA, e solo dopo si riavvia;
  2. il riavvio e' di sistema (busctl/reboot), non RestartDevice();
  3. ogni passo e' tracciato in /opt/plcnext/user/switch.state, cosi' uno script
     sull'host (switch-watchdog.sh) puo' recuperare un boot che non completa;
  4. nessun file viene cancellato senza verificarne l'esito.

Il container muore col reboot: la parte "post-riavvio" NON puo' stare qui.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)

# impostato anche da main.py (project_switch.APP_PROJECT_NAME = ...)
APP_PROJECT_NAME = 'AxisControlPanel'

PROJECTS_DIR = '/opt/plcnext/projects'
CURRENT_LINK = os.path.join(PROJECTS_DIR, 'current')
USER_DIR = '/opt/plcnext/user'
STATE_PATH = os.path.join(USER_DIR, 'switch.state')
# Il componente ACF (registrazione "manuale", sempre presente o assente in modo
# deterministico): si registra SOLO tramite questo file, mai dentro il progetto.
# Layout accertato sul controller (FW 2026.6):
#   /etc/plcnext/device/Default.acf.config  (sistema, root:plcnext_firmware 0640)
#     +-- Include *.acf.config                      -> SOLO file di sistema
#     +-- Include $ARP_PATH_APPS_ACF_CONFIGS_DIR$   -> app PLCnext
#     +-- Include $ARP_PATH_PROJECT_DEFAULT_DIR$/Default.acf.config  (traits Custom)
#           e /opt/plcnext/projects/Default/Default.acf.config fa:
#             Include *.acf.config                          -> LEGGE TriggerRetain.acf.config
#             Include $ARP_PATH_PROJECT_CURRENT_DIR$/*.acf.config
# Quindi il punto di registrazione corretto e' /opt/plcnext/projects/Default/,
# NON /etc/plcnext/device/ (li' gli include prendono solo file di sistema).
ACF_INCLUDE_DIR = os.path.join(PROJECTS_DIR, 'TriggerRetain')
# ATTENZIONE (FW 2026.6): il processo principale gira con
#   --config=/etc/plcnext/device/Default.acf.config
# quindi la config ACF di SISTEMA sta in /etc/plcnext/device/ e i file messi in
# /opt/plcnext/projects/Default/ NON vengono sempre letti. Si installano in
# entrambi i punti e si sceglie quello che il firmware usa davvero
# (vedi resolve_acf_targets()).
# /etc/plcnext/device/ NON va usata per registrare componenti: i suoi include
# prendono solo file di sistema (traits="System"). Serve solo per diagnosi.
SYSTEM_ACF_DIR = '/etc/plcnext/device'
DEFAULT_DIR = os.path.join(PROJECTS_DIR, 'Default')
# Rete di sicurezza indipendente dall'ACF di sistema: un *.acf.config nella
# RADICE del progetto attivo viene incluso da
#   <Include path="$ARP_PATH_PROJECT_CURRENT_DIR$/*.acf.config" />
ACF_PROJECT_ROOT_CONFIG = 'TriggerRetain.acf.config'
# Nome del progetto utente: si scopre dal symlink, non si hardcoda.
FALLBACK_USER_PROJECT = 'PCWE'
BOOT_TIMEOUT_SEC = 180


# --------------------------------------------------------------------------- #
# utilita' di base
# --------------------------------------------------------------------------- #

def run(cmd, check=False, timeout=60):
    """Esegue un comando loggando stdout/stderr. Ritorna CompletedProcess."""
    logger.info('exec: %s', cmd)
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             executable='/bin/bash', timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error('timeout ( %ss ) eseguendo: %s', timeout, cmd)
        return None
    if res.stdout.strip():
        logger.info('  stdout: %s', res.stdout.strip()[:2000])
    if res.stderr.strip():
        logger.warning('  stderr: %s', res.stderr.strip()[:2000])
    if check and res.returncode != 0:
        raise RuntimeError('comando fallito (%s): %s' % (res.returncode, cmd))
    return res


def sync_fs():
    run('sync')
    time.sleep(0.5)


def current_target():
    try:
        return os.path.realpath(CURRENT_LINK)
    except OSError:
        return ''


def discover_user_project():
    """
    Nome del progetto utente = cartella in /opt/plcnext/projects che NON e'
    quella dell'app e NON e' una cartella di sistema. Cosi' non si hardcoda PCWE.
    """
    candidates = []
    try:
        for name in sorted(os.listdir(PROJECTS_DIR)):
            full = os.path.join(PROJECTS_DIR, name)
            if not os.path.isdir(full):
                continue
            if name in ('Default', 'Dynamic', 'Safemode', 'Splc', 'SPlc',
                        'PLCnextBase'):
                continue
            if name == APP_PROJECT_NAME:
                continue
            candidates.append(name)
    except OSError as exc:
        logger.error('impossibile leggere %s: %s', PROJECTS_DIR, exc)
        return FALLBACK_USER_PROJECT
    if len(candidates) == 1:
        return candidates[0]
    if FALLBACK_USER_PROJECT in candidates:
        return FALLBACK_USER_PROJECT
    logger.warning('progetto utente ambiguo: %s', candidates)
    return candidates[0] if candidates else FALLBACK_USER_PROJECT


# --------------------------------------------------------------------------- #
# stato (per il watchdog sull'host)
# --------------------------------------------------------------------------- #

def runtime_active():
    """Il servizio runtime PLCnext e' attivo? (systemd su FW >= 2022.0)."""
    res = run('systemctl is-active plcnext', timeout=10)
    return bool(res is not None and res.returncode == 0
                and res.stdout.strip() == 'active')


def runtime_restarted_after(ts):
    """
    Prova che il runtime si e' riavviato DOPO `ts` (epoch). Serve a capire se il
    reboot richiesto ha avuto effetto: su questo controller
        systemctl show -p ActiveEnterTimestamp --value plcnext
    cambia ad ogni restart del servizio, che avviene al boot.
    """
    res = run("systemctl show -p ActiveEnterTimestamp --value plcnext", timeout=10)
    if res is None or res.returncode != 0 or not res.stdout.strip():
        return None
    stamp = res.stdout.strip()
    # formato: "Thu 2026-11-19 17:33:14 UTC"
    res2 = run('date -d "%s" +%%s' % stamp, timeout=10)
    if res2 is None or res2.returncode != 0:
        return None
    try:
        return int(res2.stdout.strip()) >= int(ts)
    except ValueError:
        return None


def _service_active_seconds():
    """
    Da quanti secondi il servizio plcnext e' attivo. Al boot e' ~0, quindi e' il
    modo piu' affidabile di sapere se il riavvio e' avvenuto davvero.
    """
    res = run("systemctl show -p ActiveEnterTimestampMonotonic --value plcnext",
              timeout=10)
    if res is not None and res.returncode == 0 and res.stdout.strip():
        try:
            start_us = int(res.stdout.strip())  # microsecondi da boot
        except ValueError:
            start_us = None
        if start_us:
            try:
                with open('/proc/uptime', 'r', encoding='utf-8') as handle:
                    uptime = float(handle.read().split()[0])
                return max(0.0, uptime - start_us / 1_000_000.0)
            except (OSError, ValueError):
                pass
    return None


def host_wbm_ok():
    """Il WBM risponde sull'host? (aggira eventuali limiti del container)."""
    import urllib.request
    for url in ('http://127.0.0.1/wbm/', 'http://localhost/wbm/'):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            continue
    res = run('curl -sf -m 5 -o /dev/null http://127.0.0.1/wbm/', timeout=15)
    return bool(res is not None and res.returncode == 0)


def wait_for_system_back(timeout=240):
    """
    Attende che il WBM torni a rispondere (vero indicatore di "firmware pronto":
    il 502 significa che non lo e'). Ritorna True se e' tornato.
    """
    import urllib.request
    started = time.time()
    while time.time() - started < timeout:
        try:
            with urllib.request.urlopen('http://127.0.0.1/wbm/', timeout=5) as resp:
                if resp.status < 500:
                    logger.info('WBM raggiungibile dopo %ds', int(time.time() - started))
                    return True
        except Exception:
            pass
        time.sleep(5)
    logger.error('WBM non raggiungibile dopo %ds', timeout)
    return False


def wait_for_runtime_applied(timeout=120):
    """
    Da chiamare SOLO se il processo e' ancora vivo dopo un reboot richiesto.
    Significa che il riavvio NON e' stato applicato (tipico: permessi mancanti).
    In quel caso si fa il restart del solo runtime e si verifica che il servizio
    sia ripartito dopo la richiesta.
    """
    started = int(time.time())
    while time.time() - started < timeout:
        if not runtime_active():
            time.sleep(3)
            continue
        applied = runtime_restarted_after(started - 5)
        if applied:
            logger.info('runtime riavviato: nuovo progetto applicato')
            return True
        time.sleep(3)

    logger.error('il riavvio non e\' stato applicato: forzo il restart del runtime')
    cmd = runtime_restart_command()
    if cmd:
        run('sudo -n ' + cmd, timeout=180)
        time.sleep(25)
        if runtime_active():
            return True
    return False


def verify_boot_after_switch():
    """
    Da chiamare all'avvio dell'app (che gira DOPO il riavvio richiesto dallo
    switch). Confronta il progetto attivo con quello richiesto e scrive l'esito
    reale in switch.state: e' l'unico punto in cui si sa per certo se il nuovo
    progetto e' stato caricato o se il controller e' rimasto su quello vecchio.
    """
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None

    if state.get('phase') not in ('pending', 'failed'):
        return None
    if state.get('step') != 'reboot':
        return None

    wanted = state.get('target_project') or ''
    actual = os.path.realpath(CURRENT_LINK)
    loaded = bool(wanted) and actual == os.path.realpath(
        os.path.join(PROJECTS_DIR, wanted))

    state['verified_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    state['actual_project'] = actual
    if loaded:
        state['phase'] = 'done'
        state['step'] = 'boot-verified'
        state['error'] = ''
        logger.info('switch verificato: il progetto attivo e\' %s', wanted)
    else:
        state['phase'] = 'failed'
        state['step'] = 'boot-verified'
        state['error'] = ('progetto richiesto %s ma current punta a %s'
                          % (wanted, actual))
        logger.error('switch NON riuscito: richiesto %s, attivo %s', wanted, actual)
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
        sync_fs()
    except OSError as exc:
        logger.error('impossibile aggiornare switch.state: %s', exc)
    return state


def write_state(phase, target_project, step='', error=''):
    state = {
        'phase': phase,                # idle | pending | done | failed
        'target_project': target_project,
        'step': step,
        'error': error,
        'updated': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'deadline': int(time.time()) + BOOT_TIMEOUT_SEC,
        'boot_timeout_sec': BOOT_TIMEOUT_SEC,
        'pid': os.getpid(),
    }
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
        sync_fs()
        logger.info('switch.state -> %s (step=%s)', phase, step)
    except OSError as exc:
        logger.error('impossibile scrivere %s: %s', STATE_PATH, exc)


def report_switch_marker():
    """
    Il marker /opt/plcnext/user/BackupAction.txt NON si cancella da qui.

    Chi lo scrive e' il componente ACF TriggerRetain all'avvio del firmware e
    chi lo consuma e' il componente stesso al boot successivo: se il container
    lo cancella mentre lo switch e' in corso, il trigger `backup`/`restore`
    viene perso (era la causa del `restore` inerte). Qui si registra soltanto,
    per poterlo correlare con il boot.
    """
    path = os.path.join(USER_DIR, 'BackupAction.txt')
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                logger.info('BackupAction.txt presente all\'inizio dello switch: %r',
                            handle.read().strip())
        else:
            logger.info('BackupAction.txt assente all\'inizio dello switch')
    except OSError as exc:
        logger.warning('BackupAction.txt illeggibile: %s', exc)


# --------------------------------------------------------------------------- #
# riavvio
# --------------------------------------------------------------------------- #

def runtime_restart_command():
    """Comando per riavviare il runtime PLCnext, scoperto a runtime.
    Da FW 2022.0 il runtime e' il servizio systemd `plcnext`; su firmware piu'
    vecchi esisteva /etc/init.d/plcnext. Su alcuni target (es. iC92xx) il nome puo'
    differire, quindi si prova in ordine."""
    candidates = [
        ('systemctl restart plcnext', '/usr/bin/systemctl'),
        ('/etc/init.d/plcnext restart', '/etc/init.d/plcnext'),
        ('systemctl restart plcnext-runtime', '/usr/bin/systemctl'),
    ]
    for cmd, marker in candidates:
        if os.path.exists(marker):
            return cmd
    return None


def reboot_system():
    """
    Riavvio di SISTEMA, non RestartDevice():
      1. busctl -> logind (CanReboot ha risposto "yes" su questo controller)
      2. systemctl reboot / reboot
      3. restart del solo runtime (systemctl restart plcnext, ex /etc/init.d/plcnext)
         come ultima risorsa: rimedio documentato da Phoenix per il caso
         "restart richiesto da un app che non parte bene".
    """
    commands = [
        'busctl call org.freedesktop.login1 /org/freedesktop/login1 '
        'org.freedesktop.login1.Manager Reboot b true',
        'systemctl reboot',
        '/sbin/reboot -f',
        'reboot',
    ]
    for cmd in commands:
        res = run(cmd, timeout=20)
        if res is not None and res.returncode == 0:
            logger.info('riavvio richiesto con: %s', cmd)
            return True
        logger.warning('riavvio NON riuscito con: %s', cmd)

    runtime_cmd = runtime_restart_command()
    if runtime_cmd:
        logger.error('nessun reboot disponibile: provo "%s"', runtime_cmd)
        res = run('sudo -n ' + runtime_cmd, timeout=180)
        return bool(res is not None and res.returncode == 0)

    logger.error('nessun metodo di riavvio disponibile: il cambio progetto sara'
                 ' effettivo al prossimo reboot manuale')
    return False


# --------------------------------------------------------------------------- #
# verifica del progetto prima di toccare il symlink
# --------------------------------------------------------------------------- #

# Parti del progetto che dipendono dall'HARDWARE (assi, fieldbus, motion kernel)
# e che devono venire dal progetto utente, non dal template: senza di loro il
# progetto parte ma i task vanno in watchdog.
HARDWARE_PROFILE_PATHS = (
    'Io',                              # tutti i master/fieldbus: Ecat, MotionAxes, SliceBus, PnC
    'Services/FMK',                    # Motion Kernel
    'Plc/DriveParam',
    'Plc/DefaultDriveParam',
    'Plc/MC/startup/axis.xml',
    'Plc/MC/startup/servonet.xml',
    'Plc/MC/startup/group.xml',
    # Plc/Esm/PCWE.esm.config NON si tocca: i tempi dei task sono una scelta di
    # progetto (real-time della macchina) e non vengono modificati da qui.
)

REQUIRED_ENTRIES = [
    'PCWE.manifest.config',
    'Plc/Domain',
    'Libs',
]


# Sottocartelle del progetto che NON fanno parte della catena di include ACF:
#   Libs/TriggerRetain/  -> residuo del template, non incluso (lo include il
#                           Default.acf.config di sistema, non quello del progetto)
#   Libs/PLCnextBase*    -> portate dal firmware / da PLCnextBase, non dal progetto
ACF_SCAN_EXCLUDE = (
    os.path.join('Libs', 'TriggerRetain'),
    os.path.join('Libs', 'PLCnextBase'),
    os.path.join('Libs', 'PLCnextBaseServices'),
)


def _expand_arp(path_token):
    """Espande le variabili usate nei config PLCnext (dentro il container)."""
    token = path_token
    token = token.replace('$ARP_PROJECTS_DIR$', '/opt/plcnext/projects')
    token = token.replace('$ARP_PATH_PROJECT_DEFAULT_DIR$',
                          '/opt/plcnext/projects/Default')
    token = token.replace('$ARP_PATH_PROJECT_CURRENT_DIR$', current_target())
    return token


def verify_project_tree(path):
    """
    Verifica il progetto che sta per essere attivato.

    Ritorna (bloccanti, avvisi):
      - bloccanti: impediscono lo switch (progetto incompleto o libreria che il
        firmware cercherebbe e non troverebbe);
      - avvisi: annotati nel log ma NON bloccano (config fuori dal percorso di
        include, oppure path che non si possono verificare dal container).
    """
    blocking = []
    warnings = []
    if not os.path.isdir(path):
        return ['la cartella progetto non esiste: %s' % path], warnings

    for rel in REQUIRED_ENTRIES:
        if not os.path.exists(os.path.join(path, rel)):
            blocking.append('manca %s' % rel)

    if not os.path.exists(os.path.join(path, 'Plc', 'Eclr', 'eCLR.img')):
        blocking.append('manca Plc/Eclr/eCLR.img (programma PLC assente)')

    # gli hash del manifest sono l'unica verifica che il firmware fa davvero:
    # se non tornano, il progetto non viene applicato e resta quello precedente
    m_blocking, m_warnings = verify_project_manifest(path)
    blocking.extend(m_blocking)
    warnings.extend(m_warnings)

    excluded = tuple(os.path.normpath(os.path.join(path, rel))
                     for rel in ACF_SCAN_EXCLUDE)

    for root, _dirs, files in os.walk(path):
        if os.path.normpath(root).startswith(excluded):
            continue
        for name in files:
            if not name.endswith('.acf.config'):
                continue
            cfg = os.path.join(root, name)
            rel_cfg = os.path.relpath(cfg, path)
            try:
                with open(cfg, 'r', encoding='utf-8', errors='replace') as handle:
                    text = handle.read()
            except OSError as exc:
                warnings.append('illeggibile %s (%s)' % (rel_cfg, exc))
                continue

            for token in _binary_paths(text):
                if token.startswith('$ARP_') or token.startswith('/'):
                    resolved = _expand_arp(token)
                    # l'esterno del container (host) non e' verificabile da qui
                    if not os.path.exists(resolved):
                        warnings.append('%s: %s non verificabile dal container '
                                        '(o inesistente)' % (rel_cfg, token))
                    continue
                binary = os.path.normpath(os.path.join(root, token))
                if not os.path.exists(binary):
                    blocking.append('%s punta a %s che non esiste'
                                    % (rel_cfg, token))

    return blocking, warnings



def verify_project_manifest(path):
    """
    Verifica il manifest del progetto (PCWE.manifest.config).

    Il firmware PLCnext valida il progetto confrontando lo SHA512 di ogni file
    con quello scritto nel manifest: se un solo hash non torna, il progetto non
    viene applicato e /opt/plcnext/projects/current resta sul progetto
    precedente ("non carica il progetto template, resta quello utente").

    Caso reale trovato: 33 file su 52 normalizzati a LF mentre il manifest era
    stato generato con CRLF -> manifest invalido.

    Ritorna (bloccanti, avvisi).
    """
    blocking = []
    warnings = []
    manifest = os.path.join(path, 'PCWE.manifest.config')
    if not os.path.exists(manifest):
        warnings.append('PCWE.manifest.config assente: impossibile validare '
                        'gli hash del progetto')
        return blocking, warnings
    try:
        with open(manifest, 'r', encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except OSError as exc:
        warnings.append('manifest illeggibile: %s' % exc)
        return blocking, warnings

    entries = re.findall(r'<File\s+path="([^"]+)"\s+hash="([0-9a-fA-F]+)"', text)
    if not entries:
        warnings.append('manifest senza elenco file: nessuna verifica possibile')
        return blocking, warnings

    mismatched = []
    for rel, expected in entries:
        full = os.path.join(path, *rel.split('/'))
        if not os.path.exists(full):
            blocking.append('manifest cita %s, assente nel progetto' % rel)
            continue
        try:
            with open(full, 'rb') as handle:
                actual = hashlib.sha512(handle.read()).hexdigest()
        except OSError as exc:
            warnings.append('hash non calcolabile per %s (%s)' % (rel, exc))
            continue
        if actual.lower() != expected.lower():
            mismatched.append(rel)

    if mismatched:
        blocking.append(
            'manifest non valido: %d file su %d hanno hash diverso da quello '
            'atteso (%s%s). Il firmware rifiutera' % (
                len(mismatched), len(entries), ', '.join(mismatched[:5]),
                ', ...' if len(mismatched) > 5 else '') +
            ' il progetto: quasi sempre sono stati riscritti con fine riga '
            'LF invece di CRLF.')

    overall = re.search(r'overallHash="([0-9a-fA-F]+)"', text)
    if overall:
        joined = ''.join(h for _p, h in entries).encode('ascii')
        if hashlib.sha512(joined).hexdigest().lower() != overall.group(1).lower():
            blocking.append('overallHash del manifest non coerente con gli hash '
                            'dei singoli file')
    return blocking, warnings


def _binary_paths(xml_text):
    out = []
    marker = 'binaryPath="'
    start = 0
    while True:
        idx = xml_text.find(marker, start)
        if idx < 0:
            return out
        idx += len(marker)
        end = xml_text.find('"', idx)
        if end < 0:
            return out
        out.append(xml_text[idx:end])
        start = end


# --------------------------------------------------------------------------- #
# installazione del componente ACF (registrazione sempre deterministica)
# --------------------------------------------------------------------------- #

def resolve_acf_targets():
    """
    Il punto di registrazione e' /opt/plcnext/projects/Default/:
    il Default.acf.config di sistema include il Default.acf.config del progetto,
    che a sua volta fa <Include path="*.acf.config" /> in quella cartella.
    (Verificato sul controller il 19/11: e' esattamente cosi'.)
    """
    if os.path.isdir(DEFAULT_DIR):
        return [DEFAULT_DIR]
    logger.error('%s non esiste: impossibile registrare il componente', DEFAULT_DIR)
    return []


LIBRARY_ACF = """<?xml version="1.0" encoding="UTF-8"?>
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
"""

INCLUDE_ACF = """<?xml version="1.0" encoding="utf-8"?>
<AcfConfigurationDocument
  xmlns="http://www.phoenixcontact.com/schema/acfconfig"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.phoenixcontact.com/schema/acfconfig.xsd"
  schemaVersion="1.0" >
  <Includes>
    <Include path="$ARP_PROJECTS_DIR$/TriggerRetain/*.acf.config" />
  </Includes>
</AcfConfigurationDocument>
"""


def install_acf_component(trigger_retain_dir, project_root=None):
    """
    Registra il componente UNA volta, in modo deterministico.

    Layout (verificato sul controller):
      /opt/plcnext/projects/TriggerRetain/
          lib/libTriggerRetain.so              <- binario
          TriggerRetainLibrary.acf.config      <- libreria + componente (path assoluto)
      /opt/plcnext/projects/Default/
          TriggerRetain.acf.config             <- include (letto da Default.acf.config)

    Il file TriggerRetain/Library.acf.config che sta nel repo usa il path relativo
    "./lib/..." (come la guida Phoenix), ma viene letto da Default/ e quindi
    risolverebbe in Default/lib/... : qui si scrive la versione con path assoluto.

    `project_root` e' accettato per compatibilita' ma NON si usa piu': un
    *.acf.config nella radice del progetto verrebbe incluso anche quando il
    progetto app non e' attivo.
    """
    lib_src = os.path.join(trigger_retain_dir, 'lib', 'libTriggerRetain.so')
    if not os.path.exists(lib_src):
        logger.error('libTriggerRetain.so non trovato in %s', lib_src)
        return False

    targets = resolve_acf_targets()
    if not targets:
        return False

    try:
        os.makedirs(os.path.join(ACF_INCLUDE_DIR, 'lib'), exist_ok=True)
        shutil.copy2(lib_src, os.path.join(ACF_INCLUDE_DIR, 'lib', 'libTriggerRetain.so'))
        with open(os.path.join(ACF_INCLUDE_DIR, 'TriggerRetainLibrary.acf.config'),
                  'w', encoding='utf-8') as handle:
            handle.write(LIBRARY_ACF)
        with open(os.path.join(ACF_INCLUDE_DIR, 'TriggerRetain.acf.config'),
                  'w', encoding='utf-8') as handle:
            handle.write(INCLUDE_ACF)
        for target in targets:
            with open(os.path.join(target, 'TriggerRetain.acf.config'),
                      'w', encoding='utf-8') as handle:
                handle.write(INCLUDE_ACF)
            logger.info('componente ACF registrato in %s', target)
        sync_fs()
    except OSError as exc:
        logger.error('installazione componente ACF fallita: %s', exc)
        return False

    return verify_acf_component()


def verify_acf_component():
    """Verifica che i file che il firmware leggera' esistano e siano coerenti."""
    problems = []
    include = os.path.join(DEFAULT_DIR, 'TriggerRetain.acf.config')
    library = os.path.join(ACF_INCLUDE_DIR, 'TriggerRetainLibrary.acf.config')
    binary = os.path.join(ACF_INCLUDE_DIR, 'lib', 'libTriggerRetain.so')

    for path in (include, library, binary):
        if not os.path.exists(path):
            problems.append('manca %s' % path)

    # deve essere leggibile dal firmware (gira come plcnext_firmware)
    for path in (include, library, binary):
        if os.path.exists(path) and not os.access(path, os.R_OK):
            problems.append('%s non leggibile' % path)

    # l'include deve puntare a una cartella che contiene davvero la libreria
    include_path = os.path.join(DEFAULT_DIR, 'TriggerRetain.acf.config')
    try:
        with open(include_path, 'r', encoding='utf-8', errors='replace') as handle:
            text = handle.read()
        if 'TriggerRetain' not in text:
            problems.append('%s non include TriggerRetain' % include_path)
    except OSError as exc:
        problems.append('illeggibile %s (%s)' % (include_path, exc))

    if problems:
        logger.error('verifica componente ACF fallita: %s', '; '.join(problems))
        return False

    logger.info('componente ACF verificato: %s + %s', include, library)
    return True


# --------------------------------------------------------------------------- #
# le due direzioni dello switch
# --------------------------------------------------------------------------- #

def finish_switch(target_project):
    """
    Chiude lo switch chiedendo il riavvio.

    Confine del riavvio: subito PRIMA si scrive lo stato, si registra l'istante
    di boot e si marca un sentinel; subito DOPO si controlla se il sentinel e'
    sopravvissuto al riavvio (sparisce al boot successivo, perche' sta in /tmp).

      - sentinel sparito  -> il sistema si e' riavviato: si attende il WBM e si
                             marca "done";
      - sentinel presente -> il riavvio NON e' stato applicato: si fa il restart
                             del solo runtime (rimedio documentato) e si verifica.

    In entrambi i casi il container muore e riparte col nuovo progetto.
    """
    # --- prima del riavvio ---
    write_state('pending', target_project, step='reboot')
    sync_fs()
    before_uptime = None
    try:
        with open('/proc/uptime', 'r', encoding='utf-8') as handle:
            before_uptime = float(handle.read().split()[0])
    except (OSError, ValueError):
        pass
    # sentinel: se il sistema si riavvia sparisce (limite: NON e' persistente,
    # quindi non e' una prova definitiva: la prova vera e' l'uptime che torna
    # indietro o il servizio plcnext "giovane")
    run('rm -f /tmp/axis-control-switch-sentinel /var/tmp/axis-control-switch-sentinel')
    run('touch /tmp/axis-control-switch-sentinel')

    rebooted_request = reboot_system()

    # --- dopo il riavvio (se siamo ancora vivi) ---
    surfived_sentinel = os.path.exists('/tmp/axis-control-switch-sentinel')
    after_uptime = None
    try:
        with open('/proc/uptime', 'r', encoding='utf-8') as handle:
            after_uptime = float(handle.read().split()[0])
    except (OSError, ValueError):
        pass

    uptime_went_back = (before_uptime is not None and after_uptime is not None
                        and after_uptime < before_uptime)
    service_age = _service_active_seconds()

    reboot_happened = (not surfived_sentinel) or uptime_went_back or (
        service_age is not None and service_age < 300)

    logger.info('finish_switch: reboot richiesto=%s, sentinel_presente=%s, '
                'uptime_prima=%s dopo=%s, eta_servizio=%s -> riavvio=%s',
                rebooted_request, surfived_sentinel, before_uptime, after_uptime,
                service_age, reboot_happened)

    if reboot_happened:
        # il container puo' ripartire prima del runtime: si concede tempo
        time.sleep(10)
        ok = wait_for_system_back() or host_wbm_ok()
        write_state('done' if ok else 'failed', target_project,
                    step='reboot-verified',
                    error='' if ok else 'WBM non raggiungibile dopo il riavvio')
        return ok, 'riavvio effettuato' if ok else 'riavvio effettuato ma WBM non risponde'

    # il sistema non si e' riavviato: il progetto non e' ancora attivo
    logger.warning('reboot NON applicato: uso il restart del solo runtime')
    if wait_for_runtime_applied():
        ok = wait_for_system_back(timeout=120) or host_wbm_ok()
        write_state('done' if ok else 'failed', target_project,
                    step='runtime-restart',
                    error='' if ok else 'WBM non raggiungibile dopo restart runtime')
        return ok, 'runtime riavviato' if ok else 'runtime riavviato, WBM non risponde'
    write_state('failed', target_project, step='reboot',
                error='riavvio non applicato e restart runtime fallito')
    return False, 'riavvio non applicato (serve sudo/permessi reboot)'


def activate_app_project(app_name, template_dir, trigger_retain_dir,
                         user_project=None):
    """utente -> template. Ritorna (ok, messaggio)."""
    target = os.path.join(PROJECTS_DIR, app_name)
    user_project = user_project or discover_user_project()
    source = os.path.join(PROJECTS_DIR, user_project)

    write_state('pending', app_name, step='prepare')
    report_switch_marker()

    # 1. staging: copia in una cartella temporanea, mai direttamente sul target
    staging = target + '.staging'
    try:
        if os.path.exists(staging):
            shutil.rmtree(staging)
        if not os.path.exists(target):
            run('cp -a "%s" "%s"' % (template_dir, staging), check=True, timeout=600)
        elif shutil.which('rsync'):
            # riallineamento del template (mantiene eventuali dati locali)
            run('rsync -a --delete "%s/" "%s/"' % (template_dir, staging),
                check=True, timeout=600)
        else:
            # rsync NON e' presente nell'immagine python:3.9-slim: si rifa' la
            # copia da zero. La staging e' sempre ricreabile, quindi va bene.
            logger.info('rsync assente: ricreo la staging con cp -a')
            shutil.rmtree(staging, ignore_errors=True)
            run('cp -a "%s" "%s"' % (template_dir, staging), check=True, timeout=600)

        # 2. profilo hardware/config dal progetto utente.
        #
        # NON basta piu' copiare solo Io/Arp.Io.Ecat + axis.xml: il GDS del
        # template referenzia Arp.Services.FMK, Arp.Io.SliceBus, Arp.Io.MotionAxes
        # e le drive param, che nel template NON esistono. Con i connettori a
        # vuoto i task sforano il watchdog:
        #   "Watchdog of task 'FastTask' triggered (watchdog 8000000us / 13400us)"
        #   -> PlcState Stop|Error -> WBM 502.
        # Si sincronizza quindi l'intero profilo I/O e motion del progetto utente.
        for rel in HARDWARE_PROFILE_PATHS:
            src = os.path.join(source, rel)
            dst = os.path.join(staging, rel)
            if not os.path.exists(src):
                logger.warning('profilo hardware: %s assente nel progetto utente, salto', rel)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            run('cp -a "%s" "%s"' % (src, os.path.dirname(dst) + '/'), check=True)
            logger.info('profilo hardware: copiato %s',
                        os.path.basename(src) or rel)

        # 3. TriggerRetain dentro il progetto: NON si tocca.
        #    Vive in <progetto>/Libs/TriggerRetain/ ed e' referenziato anche da
        #    Plc/Meta/PCWE.meta.config; non e' nel percorso degli include, quindi
        #    non provoca doppia registrazione. Rimuoverlo puo' rompere il meta.

        # ownership coerente con i progetti creati dal firmware
        run('chown -R 1002:1002 "%s"' % staging)
        sync_fs()

        # 4. VERIFICA prima di toccare il symlink
        blocking, warn = verify_project_tree(staging)
        for item in warn:
            logger.warning('verifica (non bloccante): %s', item)
        if blocking:
            msg = 'progetto non valido: ' + '; '.join(blocking)
            logger.error(msg)
            write_state('failed', app_name, step='verify', error=msg)
            # la staging non si butta: serve per capire, e il prossimo tentativo
            # la ricrea comunque
            return False, msg

        # 5. aggiornamento IN PLACE del progetto (niente mv/rename su progetti
        #    che il firmware puo' avere aperto: un rename mentre il boot legge i
        #    file crea incoerenze difficili da spiegare). Il symlink si tocca
        #    solo se non punta gia' al progetto.
        if os.path.exists(target):
            backup_dir = target + '.previous'
            shutil.rmtree(backup_dir, ignore_errors=True)
            try:
                # copia di sicurezza del progetto attualmente in uso
                run('cp -a "%s" "%s"' % (target, backup_dir), timeout=900)
                run('rsync -a --delete "%s/" "%s/"' % (staging, target)
                    if shutil.which('rsync')
                    else 'cp -a "%s/." "%s/"' % (staging, target), check=True,
                    timeout=900)
                logger.info('progetto %s aggiornato in place', target)
            except Exception:
                logger.exception('aggiornamento in place fallito: ripristino la copia')
                run('rsync -a --delete "%s/" "%s/"' % (backup_dir, target)
                    if shutil.which('rsync')
                    else 'cp -a "%s/." "%s/"' % (backup_dir, target), timeout=900)
                run('rm -rf "%s"' % staging)
                raise
            shutil.rmtree(staging, ignore_errors=True)
        else:
            run('mv "%s" "%s"' % (staging, target), check=True)

        if current_target() != os.path.realpath(target):
            run('ln -sfn "%s" "%s"' % (target, CURRENT_LINK), check=True)
            sync_fs()

        if current_target() != os.path.realpath(target):
            msg = 'symlink non aggiornato (current -> %s)' % current_target()
            write_state('failed', app_name, step='symlink', error=msg)
            return False, msg

        # 6. componente ACF: registrazione sempre presente, una sola
        install_acf_component(trigger_retain_dir, project_root=target)

        # la copia di sicurezza si elimina solo a switch completato
        shutil.rmtree(target + '.previous', ignore_errors=True)
        return finish_switch(target)
    except Exception as exc:  # noqa: BLE001 - vogliamo lo stato su disco
        msg = 'errore attivazione: %s' % exc
        logger.exception(msg)
        write_state('failed', app_name, step='activate', error=msg)
        return False, msg


def activate_user_project(app_name, user_project=None):
    """template -> utente. Ritorna (ok, messaggio)."""
    user_project = user_project or discover_user_project()
    target = os.path.join(PROJECTS_DIR, user_project)
    app_dir = os.path.join(PROJECTS_DIR, app_name)

    write_state('pending', user_project, step='prepare')
    report_switch_marker()

    if not os.path.isdir(target):
        msg = 'progetto utente assente: %s' % target
        write_state('failed', user_project, step='verify', error=msg)
        return False, msg

    blocking, warn = verify_project_tree(target)
    for item in warn:
        logger.warning('verifica progetto utente (non bloccante): %s', item)
    if blocking:
        msg = 'progetto utente non valido: ' + '; '.join(blocking)
        logger.error(msg)
        write_state('failed', user_project, step='verify', error=msg)
        return False, msg

    run('ln -sfn "%s" "%s"' % (target, CURRENT_LINK), check=True)
    sync_fs()

    if current_target() != os.path.realpath(target):
        msg = 'symlink non aggiornato (current -> %s)' % current_target()
        write_state('failed', user_project, step='symlink', error=msg)
        return False, msg

    # il progetto app resta in place: e' ricreabile e non va toccato col symlink
    logger.info('progetto app mantenuto in %s per il prossimo switch', app_dir)

    # registrazione ACF valida per ENTRAMBI i progetti (vive in projects/Default)
    install_acf_component(APP_TRIGGER_RETAIN_DIR)

    return finish_switch(target)
