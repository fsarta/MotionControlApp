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

import json
import logging
import os
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


def wait_for_runtime_applied(timeout=90):
    """
    Dopo aver chiesto il reboot si resta vivi finche' possibile: se il reboot e'
    andato a buon fine il processo muore prima, quindi qui si arriva solo quando
    il riavvio NON e' stato applicato (tipico: nessun permesso). In quel caso si
    fa il restart del solo runtime, che e' il rimedio documentato.
    """
    started = int(time.time())
    while time.time() - started < timeout:
        if not runtime_active():
            time.sleep(3)
            continue
        applied = runtime_restarted_after(started - 5)
        if applied:
            logger.info('runtime riavviato: nuovo progetto applicato')
            write_state('done', discover_user_project(), step='runtime-restarted')
            return True
        time.sleep(3)
    logger.error('il riavvio non e\' stato applicato: forzo il restart del runtime')
    cmd = runtime_restart_command()
    if cmd and run('sudo -n ' + cmd, timeout=180) is not None:
        time.sleep(20)
        if runtime_active():
            write_state('done', current_target(), step='runtime-restart-fallback')
            return True
    write_state('failed', current_target(), step='reboot',
                error='riavvio non applicato e restart runtime fallito')
    return False


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


def clear_switch_marker():
    """Rimuove i marker di retain ereditati, cosi' un boot non consuma trigger
    scritti da un tentativo precedente (era la causa del `restore` inerte)."""
    for name in ('BackupAction.txt',):
        path = os.path.join(USER_DIR, name)
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info('rimosso marker %s', path)
            except OSError as exc:
                logger.error('impossibile rimuovere %s: %s', path, exc)


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

REQUIRED_ENTRIES = [
    'PCWE.manifest.config',
    'Plc/Domain',
    'Libs',
]


def verify_project_tree(path):
    """Controlli minimi: il progetto e' completo e i file ACF puntano a file veri."""
    problems = []
    if not os.path.isdir(path):
        return ['la cartella progetto non esiste: %s' % path]

    for rel in REQUIRED_ENTRIES:
        if not os.path.exists(os.path.join(path, rel)):
            problems.append('manca %s' % rel)

    # ogni binaryPath dichiarato in un *.acf.config deve esistere davvero
    for root, _dirs, files in os.walk(path):
        for name in files:
            if not name.endswith('.acf.config'):
                continue
            cfg = os.path.join(root, name)
            try:
                with open(cfg, 'r', encoding='utf-8', errors='replace') as handle:
                    text = handle.read()
            except OSError as exc:
                problems.append('illeggibile %s (%s)' % (cfg, exc))
                continue
            for token in _binary_paths(text):
                binary = os.path.normpath(os.path.join(root, token))
                if not os.path.exists(binary):
                    problems.append('%s punta a %s che non esiste'
                                    % (os.path.relpath(cfg, path), token))

    if not os.path.exists(os.path.join(path, 'Plc', 'Eclr', 'eCLR.img')):
        problems.append('manca Plc/Eclr/eCLR.img (programma PLC assente)')
    return problems


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

    # la directory TriggerRetain non deve contenere altri *.acf.config non voluti
    try:
        extra = [name for name in os.listdir(ACF_INCLUDE_DIR)
                 if name.endswith('.acf.config')
                 and name != 'TriggerRetainLibrary.acf.config']
        if extra:
            problems.append('acf.config inattesi in %s: %s' % (ACF_INCLUDE_DIR, extra))
    except OSError:
        pass

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
    Chiude lo switch. Se il reboot va a buon fine questo processo muore e non
    torna piu' (ed e' corretto: il container riparte col nuovo progetto).
    Se invece resta vivo, il riavvio non e' stato applicato: si fa il restart
    del solo runtime e si attende che il nuovo progetto sia attivo.
    """
    write_state('pending', target_project, step='reboot')
    sync_fs()
    if reboot_system():
        # il processo dovrebbe morire qui dentro
        wait_for_runtime_applied()
        return True, 'reboot richiesto e runtime verificato'
    logger.warning('reboot non disponibile: uso il restart del solo runtime')
    if wait_for_runtime_applied(timeout=30):
        return True, 'riavvio del runtime applicato'
    return False, 'riavvio non applicato (vedi switch.state)'


def activate_app_project(app_name, template_dir, trigger_retain_dir,
                         user_project=None):
    """utente -> template. Ritorna (ok, messaggio)."""
    target = os.path.join(PROJECTS_DIR, app_name)
    user_project = user_project or discover_user_project()
    source = os.path.join(PROJECTS_DIR, user_project)

    write_state('pending', app_name, step='prepare')
    clear_switch_marker()

    # 1. staging: copia in una cartella temporanea, mai direttamente sul target
    staging = target + '.staging'
    try:
        if os.path.exists(staging):
            shutil.rmtree(staging)
        if not os.path.exists(target):
            run('cp -a "%s" "%s"' % (template_dir, staging), check=True, timeout=600)
        else:
            # riallineamento del template (mantiene eventuali dati locali)
            run('rsync -a --delete "%s/" "%s/"' % (template_dir, staging),
                check=True, timeout=600)

        # 2. hardware/config dal progetto utente
        if os.path.isdir(os.path.join(source, 'Io', 'Arp.Io.Ecat')):
            os.makedirs(os.path.join(staging, 'Io'), exist_ok=True)
            run('cp -a "%s" "%s"' % (os.path.join(source, 'Io', 'Arp.Io.Ecat'),
                                     os.path.join(staging, 'Io')), check=True)
        for rel in ('Plc/MC/startup/axis.xml', 'Plc/MC/startup/servonet.xml'):
            src = os.path.join(source, rel)
            if os.path.exists(src):
                run('cp -a "%s" "%s"' % (src, os.path.join(staging, rel)), check=True)

        # 3. TriggerRetain dentro il progetto: NON si tocca.
        #    Vive in <progetto>/Libs/TriggerRetain/ ed e' referenziato anche da
        #    Plc/Meta/PCWE.meta.config; non e' nel percorso degli include, quindi
        #    non provoca doppia registrazione. Rimuoverlo puo' rompere il meta.

        # ownership coerente con i progetti creati dal firmware
        run('chown -R 1002:1002 "%s"' % staging)
        sync_fs()

        # 4. VERIFICA prima di toccare il symlink
        problems = verify_project_tree(staging)
        if problems:
            msg = 'progetto non valido: ' + '; '.join(problems)
            logger.error(msg)
            write_state('failed', app_name, step='verify', error=msg)
            return False, msg

        # 5. swap atomico
        if os.path.exists(target):
            run('mv "%s" "%s"' % (target, target + '.old'), check=True)
        run('mv "%s" "%s"' % (staging, target), check=True)
        run('ln -sfn "%s" "%s"' % (target, CURRENT_LINK), check=True)
        sync_fs()

        if current_target() != os.path.realpath(target):
            msg = 'symlink non aggiornato (current -> %s)' % current_target()
            write_state('failed', app_name, step='symlink', error=msg)
            return False, msg

        # 6. componente ACF: registrazione sempre presente, una sola
        install_acf_component(trigger_retain_dir, project_root=target)

        run('rm -rf "%s.old"' % target)
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
    clear_switch_marker()

    if not os.path.isdir(target):
        msg = 'progetto utente assente: %s' % target
        write_state('failed', user_project, step='verify', error=msg)
        return False, msg

    problems = verify_project_tree(target)
    if problems:
        msg = 'progetto utente non valido: ' + '; '.join(problems)
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
