# Switch progetto utente ↔ progetto template: analisi del guasto

Controller: Yaskawa iC9226M-FSoE (PLCnext), FW 2026.6.
App: `AxisControlPanel` (Flask + gRPC nel container `axis-control-panel`), componente ACF `TriggerRetain`.
Sintomo: **utente → template OK** (assi gestiti); **template → utente KO**, controller da reset di fabbrica.

---

## 1. Come funziona oggi lo switch (ricostruzione dal codice)

| Fase | Codice | Effetto |
|---|---|---|
| Attivazione | `main.py:38-93` `setup_parallel_project()` | copia il template in `/opt/plcnext/projects/AxisControlPanel`, scrive `backup` in `/opt/plcnext/user/BackupAction.txt`, **cancella** `/opt/plcnext/projects/Default/TriggerRetain.acf.config`, copia `Io/Arp.Io.Ecat` + `axis.xml` + `servonet.xml` dal progetto utente, sposta il symlink `current`, `RestartDevice()` |
| Disattivazione | `main.py:95-123` `teardown_parallel_project()` | scrive `restore` in `BackupAction.txt`, symlink → `PCWE`, **ricopia** `TriggerRetain.acf.config` in `/Default/`, `RestartDevice()` |
| Retain lato PLC | `TriggerRetainComponent.cpp:68-125` | `OnPlcLoaded` → se il file dice `restore`: attende 2 s, `plcSvc->Stop()`, `PrepareLatestBackupFileForRestoring()`, `plcSvc->Start(PlcStartKind::RestoreWarm)`; `OnPlcStopping` → se il file dice `backup`: `GenerateBackupFile()` |

Quindi: **lo stato dello switch e del retain vive in un unico file di testo condiviso** (`/opt/plcnext/user/BackupAction.txt`) e in un unico pool di backup (`/opt/plcnext/retaining/backups/`, rotazione a 10 file) **condiviso dai due progetti**.

---

## 2. Perché il ritorno al progetto utente rompe il controller

### H1 (causa primaria) — `RestoreWarm` con un backup di retain di un *altro* progetto

Fatti documentati (Phoenix Contact):

- `GenerateBackupFile()` scrive un backup che contiene un header con **nome progetto, hw, fw, layout e CRC dei retain**.
- Un retain backup è ripristinabile **solo se coincidono nome progetto e Retain CRC**. Con progetto/layout diversi il ripristino non è valido.
- `PrepareLatestBackupFileForRestoring()` prepara **il file più recente** trovato nel path di backup, senza sapere a quale progetto appartiene; la validazione avviene solo alla `Start(RestoreWarm)`.
- `Start()` **viene ignorato** se la PLC non è in uno stato compatibile (`Ready/Running/Halt`).

Cosa succede realmente:

1. All'attivazione il progetto `AxisControlPanel` fa i suoi cold/warm start: **ogni avvio scrive un backup automatico** e la rotazione a 10 file **cancella i backup del progetto utente**. Quindi, quando si torna indietro, "il backup più recente" è **sempre** un backup del progetto template (o comunque uno con layout `AxisControlPanel`).
2. In modalità app il componente non c'è (include cancellato) → nessuno genera il backup "buono" del progetto utente al momento giusto: il file `backup` resta **non consumato** se `RestartDevice` non produce una `PlcStopping` graziosa, e viene sovrascritto da `restore` al teardown.
3. Al boot di ritorno: `TriggerRetain` legge `restore` → `Stop()` → `PrepareLatestBackupFileForRestoring()` (file del progetto sbagliato) → `Start(RestoreWarm)`.
   Il retain manager si trova a caricare un layout che non corrisponde al progetto `PCWE`: `LayoutMismatch`/`CrcFileMismatch`, e a seconda del FW la PLC **non raggiunge più lo stato Running** in modo stabile: è il "controller corrotto" che si recupera solo col reset di fabbrica.

Il progetto utente, tornando indietro **senza** componente, farebbe semplicemente un cold start pulito (il FW non può fare warm start con CRC diverso). È il `RestoreWarm` forzato a introdurre il guasto.

### H2 — Doppia registrazione del componente ACF (library caricata due volte)

- Il componente viene caricato dall'include `Default/TriggerRetain.acf.config` → `$ARP_PROJECTS_DIR$/TriggerRetain/*.acf.config` → `libTriggerRetain.so` da `/opt/plcnext/projects/TriggerRetain/lib/`.
- Il template però **contiene anche** `icube_project_template/Libs/TriggerRetain/TriggerRetainLibrary.acf.config` (registra `TriggerRetainComponent1`) + `TriggerRetain.libmeta` in `Plc/Meta/PCWE.meta.config`. In quel file il `binaryPath` è `./lib/libTriggerRetain.so`, ma **`Libs/TriggerRetain/lib/` non esiste** nel template: se l'ACF legge quel config, il boot fallisce per libreria non trovata; se invece esiste una copia del `.so`, la stessa libreria (stesso SONAME, path diverso) e lo stesso nome componente vengono registrati due volte → conflitto nel type system / ACF e firmware che non parte.
- Inoltre `rm -f` / `cp` su `/opt/plcnext/projects/Default/` (`main.py:72`, `main.py:113`) **non verificano l'esito**: se il filesystem non è scrivibile dall'UID 1002 il file resta, e alla riattivazione restano attivi **entrambi** gli include.
- Modificare gli include ACF a runtime va bene **solo** se seguito da un reboot completo: l'ACF è letto al boot. Qui il reboot c'è, ma lo stato dei file non è mai verificato → il sistema può avviarsi con 0 o 2 istanze del componente invece di 1.

### H3 — Progetto copiato senza atomicità né verifica

`cp -r {template} {target}` (`main.py:58`) e i `cp` di `axis.xml`/`servonet.xml`/`Io/Arp.Io.Ecat` (`main.py:75-79`):

- `run_bash()` restituisce `False` in caso di errore ma **i chiamanti lo ignorano** (`main.py:58-118`): il symlink viene spostato anche se la copia è incompleta.
- Non c'è copia su directory temporanea + rename atomico, né `sync`, né verifica di integrità prima del cambio symlink: un boot con `current` che punta a un progetto troncato non è recuperabile se non da terminale/reset.
- I file `PCWE.manifest.config` / `PCWE.software-package-manifest.json` contengono gli hash di tutti i file: copiando axis.xml/servonet.xml/Io Ecat gli hash non corrispondono più.

### H4 — Stop/Start non atteso nel thread del componente

`TriggerRetainComponent.cpp:110-124`: `Sleep(2000)` → `Stop()` → `Prepare...` → `Start(RestoreWarm)`. La `Stop()` è sincrona, ma chiamarla dalla callback `PlcLoaded` (cioè mentre il PLC manager sta ancora caricando) è una corsa: se la PLC non è ancora in `Stopped`, la `Start()` **viene ignorata** e la PLC resta ferma. In più un `Start(RestoreWarm)` che fallisce non ha retry né diagnostica persistente: al boot successivo il file `restore` è già stato cancellato (`cpp:111`), quindi lo stato è perso.

### H5 — Logica di switch dentro il container che il reboot uccide

`RestartDevice()` riavvia il dispositivo: il container muore a metà sequenza. Infatti il loop di attesa gRPC in `_do_project_switch` (`main.py:150-175`) non gira mai: `project_state='switching'` è solo in RAM del processo che viene terminato. Tutta la logica di switch (symlink, include ACF, trigger retain) è quindi **non transazionale e non verificabile**.

---

## 3. Difetti secondari (da correggere, non causano il brick)

1. `discover_axes()` (`grpc_client.py:498`) usa il path **hardcoded** `/opt/plcnext/projects/PCWE/...`: se il progetto utente si chiama diversamente, discovery vuota.
2. Nome progetto utente `PCWE` hardcoded in `main.py:75-79`, `109`, `grpc_client.py:498`. Meglio derivarlo dal symlink `current` o da una costante unica/configurabile.
3. Nel template `Plc/MC/startup/axis.xml` c'è `<axisList />` vuoto e nessuna copia di `Io/Arp.Io.Ecat`: lo switch dipende da file del progetto utente. Se l'utente rigenera il progetto, il template non è autosufficiente.
4. `requirements.txt` blocca `grpcio==1.50.0` e `protobuf==4.21.12`: ok con i tuoi `pxc_grpc`, ma da tenere allineati al FW del controller.
5. `graceful_shutdown` (`main.py:315`) esegue il teardown su **qualunque** SIGTERM (stop app dal WBM, aggiornamento FW, riavvio container): il progetto viene riportato a `PCWE` anche quando non lo si vuole (es. app fermata senza reboot volontario). È il comportamento opposto a quello che serve in caso di app **crashata**: in quel caso il controller resta con `current` → app e senza il pannello per tornare indietro.

---

## 4. Proposta di soluzione (in ordine di priorità)

### Passo 0 — diagnosi rapida sul campo (esperimento a costo zero)

Sul controller, in modalità **app attiva**, prima di tornare indietro:

```bash
# salviamo lo stato per capire chi ha ragione tra H1 e H2
ls -la /opt/plcnext/projects /opt/plcnext/projects/Default
cat /opt/plcnext/projects/Default/Default.acf.config
ls -la /opt/plcnext/projects/AxisControlPanel/Libs/TriggerRetain
ls -la /opt/plcnext/retaining /opt/plcnext/retaining/backups
cat /opt/plcnext/user/BackupAction.txt
journalctl -u plcnext -n 300 | grep -i -E "retain|acf|esm|plm|library"
```

Poi **due prove separate** (una alla volta, con backup della config):

- **Prova A (isola H1):** disabilitare del tutto il componente (`rm /opt/plcnext/projects/Default/TriggerRetain.acf.config` e `rm -f /opt/plcnext/user/BackupAction.txt`), symlink → `PCWE`, reboot. Se il ritorno funziona, la causa è il `RestoreWarm`.
- **Prova B (isola H2):** lasciare il componente e i trigger ma usare solo `start(Warm)` (nessun `Prepare...`/`RestoreWarm`). Se anche così si rompe, la causa è ACF/doppia registrazione.

### Passo 1 — smettere di usare `RestoreWarm` tra due progetti diversi

Il retain è **per progetto** (nome + CRC): un backup del progetto template non è ripristinabile nel progetto utente. Opzioni, dalla più semplice:

1. **Nessun ripristino di retain** nello switch: al ritorno, cold/warm start naturale. Il FW gestisce già il caso CRC mismatch. Si eliminano `PrepareLatestBackupFileForRestoring()` e `Start(RestoreWarm)` → il brick sparisce.
2. **Persistenza applicativa esplicita**: se servono dei valori (quote, parametri, offset), leggerli via gRPC prima dello switch e riscriverli dopo, salvandoli come JSON in `/opt/plcnext/user/`. Indipendente dal retain CRC, verificabile, e non tocca il runtime.
3. Se davvero serve il retain: il backup deve essere **per progetto** (nome file con prefisso del progetto) e il ripristino va fatto **solo se** l'header corrisponde al progetto di destinazione, con fallback a `Start(Warm)` e retry + log su file.

### Passo 2 — rendere ACF deterministico

- **Un solo** punto di registrazione: o sempre `Default/TriggerRetain.acf.config` (sempre presente, mai cancellato), o solo dentro il progetto. Mai entrambi, mai "a runtime senza verifica".
- Rimuovere dal template `Libs/TriggerRetain/TriggerRetainLibrary.acf.config` (o aggiungere `Libs/TriggerRetain/lib/libTriggerRetain.so`) perché oggi punta a un `.so` inesistente.
- Il componente deve essere **project-aware**: leggere il symlink `current` e loggare sempre progetto attivo e stato (`idle/backup/restore`), in un file suo, così una failure è diagnosticabile dopo il reboot.

### Passo 3 — switch transazionale

- Preparare il progetto in `/opt/plcnext/projects/.staging-AxisControlPanel`, verificare (file attesi presenti, `*.so` risolvibili, XML validi), poi `mv` atomico e solo dopo il symlink.
- Spostare la sequenza di switch **fuori dal container** (script in `/opt/plcnext/user/` + unit systemd "oneshot", oppure dentro il componente ACF): il container viene terminato dal reboot, quindi non può garantire la fine della sequenza.
- Scrivere un file di stato (`/opt/plcnext/user/switch.state`) con lo step raggiunto e un `rollback` automatico se il boot successivo non arriva a `Running` entro N secondi.

---

## 5. Informazioni che mi servono per chiudere la diagnosi

1. In stato di guasto, cosa mostra il WBM: PLC in **STOP/HALT**, "no project loaded", errore di load, oppure WBM irraggiungibile? (screenshot)
2. `/opt/plcnext/logs/` (o `journalctl -u plcnext`) intorno al boot di ritorno: righe con `Retain`, `RestoreWarm`, `BackupError`, `LayoutMismatch`, `CrcFileMismatch`, `ACF`, `Library`.
3. `ls -la /opt/plcnext/retaining/backups` **prima e dopo** il guasto, e i nomi dei file `.prb`.
4. Contenuto di `/opt/plcnext/projects/Default/Default.acf.config` e `ls -la /opt/plcnext/projects/`.
5. Dopo il guasto, `/opt/plcnext/user/BackupAction.txt` contiene ancora `restore`/`backup` o è vuoto?
6. Ti serve davvero conservare i valori **retain** del progetto utente attraverso lo switch, o basta che il progetto utente riparta pulito?

---

# REVISIONE 2 — dati dal controller (19/11, dopo le prove sul campo)

## Prove raccolte (output dell'utente)

| Verifica | Risultato | Conseguenza |
|---|---|---|
| `ls /opt/plcnext/projects` (app attiva) | c'è `AxisControlPanel`, **NON c'è `TriggerRetain/`** | l'include `$ARP_PROJECTS_DIR$/TriggerRetain/*.acf.config` non risolve nulla |
| `ls /opt/plcnext/projects/Default` | **non c'è `TriggerRetain.acf.config`** | il componente ACF **non era caricato** durante il test |
| `ls /opt/plcnext/retaining/backups` | **vuota** prima e dopo; solo `RetainInfo.xml` (284 B) | `GenerateBackupFile()` non ha mai prodotto un `.prb` |
| `cat /opt/plcnext/user/BackupAction.txt` dopo il guasto | `restore` | nessuno l'ha consumato → componente non attivo |
| Prova A (rimuovere trigger + symlink→PCWE + reboot, senza app) | **nessun guasto** | H1 (RestoreWarm su backup estraneo) **REFUTATA** come causa del brick |
| Sintomo | WBM **502 Bad Gateway**, ping OK, solo LED **RDY lampeggiante** | non è "PLC in stop": è il **runtime/backend web che non arriva a ready** |
| `journalctl -u plcnext` | output **vuoto** | PLCnext parte da SysV init (`/etc/init.d/plcnext`), non da systemd: i log sono in `/opt/plcnext/logs/` |

## Cosa resta in piedi

- **H1 refutata** come causa primaria: non esistono file di backup retain, quindi non c'è nessun `RestoreWarm` con layout estraneo da applicare. Il file `restore` resta inerte perché nessuno lo legge.
- **H2 refutata nella forma "doppia registrazione"**: il componente non è caricato affatto. Però resta un difetto reale: **tutto il meccanismo TriggerRetain è oggi codice morto** nel deployment attuale (cartella `TriggerRetain/` assente, `Default/TriggerRetain.acf.config` assente). Il requisito "conservare i retain del progetto utente" **non è soddisfatto**, indipendentemente dal brick.
- **H3/H4/H5 restano** come difetti strutturali (switch non transazionale, sequenza nel container che il reboot uccide), ma non spiegano il sintomo 502 + RDY.

## Nuova ipotesi H6 — il progetto utente non è auto-consistente quando torna attivo

Il sintomo (502 + solo RDY lampeggiante) dice che **il firmware/runtime non completa il boot**: niente WBM, quindi il PLC nemmeno ci arriva. I candidati, in ordine:

1. **Parte Safety (SPNS / FSoE)**: il template `icube_project_template` è una copia del progetto PCWE e **contiene la parte safety**: `Splc/Arp.Services.SpnsProxy.tic`, il task `SafetyProxyTask`, il programma `sproxy_1` (`Arp.Services.SpnsProxy`) e i connettori `Arp.Services.SpnsProxy/SPLC*` in `Plc/Gds/PCWE.gds.config:53-55`. Sul controller FSoE la parte safety vive in `/opt/plcnext/projects/SPlc` e viene riconciliata ad ogni boot. Se il safe runtime resta in uno stato/versione non coerente con il progetto non-safe che stai caricando, il runtime può rifiutarsi di diventare ready → esattamente 502 + RDY.
2. **File di configurazione incoerenti nel progetto copiato**: `PCWE.manifest.config` / `PCWE.software-package-manifest.json` contengono gli hash SHA512 di tutti i file. Copiando `axis.xml`, `servonet.xml` e `Io/Arp.Io.Ecat` sopra il template (`main.py:75-79`) gli hash non tornano più. Se un controllo di integrità interviene al boot, il progetto viene rifiutato.
3. **Configurazione IO doppia**: il template contiene già `Io/Arp.Io.PnC/controller.tic` + `Io/_s49PNIO/PNIODEV.bin` (PROFINET) e la app vi aggiunge `Io/Arp.Io.Ecat` (EtherCAT) copiata dal progetto utente, mentre GDS/ESM referenziano **entrambi**. Due master di fieldbus attivi sullo stesso progetto è un candidato forte a boot che non completa. Notare che la direzione che *funziona* (utente → app) carica questo ibrido, quindi non è detto; ma va verificato se il PCWE reale ha davvero anche PnC.
4. **Ownership/mtime mescolati in `Default/`**: scritto da `admin` e dal container (uid 1002), poi riscritto dal firmware (`PLCnextBase.acf.config` è `plcnext_firmware`, 16:30). Un file in `Default/` non leggibile/scrivibile dal firmware blocca la fase di configurazione.

## Esperimento decisivo (10 minuti, nessuna app)

Serve a separare **"è lo switch in sé"** da **"sono gli effetti collaterali dell'app"**. Con la app **ferma** (container stoppato), solo shell:

```bash
# 0. salva lo stato buono
sudo tar czf /tmp/projects-good.tgz -C /opt/plcnext projects retaining user/BackupAction.txt 2>/dev/null

# 1. utente -> app, SOLO symlink
sudo ln -sfn /opt/plcnext/projects/AxisControlPanel /opt/plcnext/projects/current
sudo reboot

# 2. verifica che sia tutto su, poi app -> utente, SOLO symlink
sudo ln -sfn /opt/plcnext/projects/PCWE /opt/plcnext/projects/current
sudo reboot
```

- Se **si rompe anche così** → la causa è nel **contenuto del progetto copiato** (safety, IO doppio, hash) o nella transizione di progetto in sé. Il codice Python è innocente.
- Se **non si rompe** → la causa sono gli **effetti collaterali dell'app** (file cancellati in `Default/`, `BackupAction.txt`, ownership, il container che muore a metà sequenza).

Variante B utile se al punto 2 torna su: ripetere il punto 2 **dopo** aver creato a mano `/opt/plcnext/user/BackupAction.txt` = `restore` **e** con la cartella `TriggerRetain` correttamente installata, per testare il componente ACF isolato.

## Cosa raccogliere al prossimo guasto (SSH, prima di qualunque reset)

```bash
# 1. log reali (NON journalctl: plcnext parte da SysV init)
sudo ls -laR /opt/plcnext/logs | head -60
sudo tar czf /tmp/logs-fault.tgz /opt/plcnext/logs /opt/plcnext/retaining /opt/plcnext/user

# 2. stato del runtime e dei servizi
ps aux | grep -i -E "arp|plcnext|ng?inx|php" | head -30
sudo /etc/init.d/plcnext status
dmesg | tail -60            # errori kernel / watchdog / OOM
sudo cat /var/log/messages 2>/dev/null | tail -100

# 3. cosa c'è dentro i due progetti (differenze)
sudo diff -rq /opt/plcnext/projects/PCWE /opt/plcnext/projects/AxisControlPanel
sudo ls -la /opt/plcnext/projects/PCWE/Splc /opt/plcnext/projects/SPlc
```

Il file che quasi certamente contiene la risposta è il log del **SystemManager/ACF** e dell'**ESM/PLM** dentro `/opt/plcnext/logs/`: lì c'è la riga esatta del perché il boot non completa.

## Requisito "retain obbligatorio" — stato attuale: non implementato

Prove: `backups/` vuota, componente non caricato, `restore` non consumato.

Per soddisfarlo davvero servono tre cose, indipendenti dal brick:

1. **Componente effettivamente installato e attivo in entrambi i progetti** (oggi manca la cartella `/opt/plcnext/projects/TriggerRetain/`).
2. **Backup per progetto**: `GenerateBackupFile()` scrive un `.prb` con nome progetto + CRC; va copiato/rinominato con il nome del progetto e il ripristino deve avvenire **solo** se l'header corrisponde al progetto di destinazione, con fallback pulito a `Start(Warm)` se `NoSuchFile`/`LayoutMismatch`/`CrcFileMismatch`.
3. **Verifica che il progetto utente abbia davvero variabili retain**: se `GenerateBackupFile()` torna `NoRetainData` (enum `BackupError=3`) la cartella resta vuota — che è compatibile con quello che osservi.

Se poi i valori da conservare sono pochi e noti (quote, offset, parametri d'asse), la via più robusta è **leggerli via gRPC prima dello switch e riscriverli dopo**, salvandoli in JSON in `/opt/plcnext/user/`: non dipende da CRC/layout, è verificabile e non tocca il runtime.

---

# REVISIONE 3 — la causa è il tipo di riavvio (dato decisivo dell'utente)

## Fatto nuovo

> 1. switch utente → template: **funziona**
> 2. **reboot manuale**
> 3. switch template → utente: **funziona perfettamente**

Quindi il codice di switch (symlink + copia + ACF) **è corretto**. Il guasto si verifica **solo** quando il riavvio è quello richiesto dall'app via gRPC (`IDeviceControlService.RestartDevice()`, `main.py:88` e `main.py:118`) e sparisce con un riavvio reale.

## Conferma documentale del sintomo

Manuale Phoenix Contact (AXC F 2152, sez. *Restart after app installation*):

- «If the PLCnext system firmware has not started up properly, the **WBM displays the error message "Bad Gateway 502"**» → è **esattamente** il sintomo osservato.
- «Sporadically it can happen that **a restart of the firmware requested by an app installation does not work properly**. If the firmware does not start up correctly, the controller can be restarted by one of the following 3 possible actions: **restart of the firmware via SSH (`/etc/init.d/plcnext restart`)**, **reboot of the controller via SSH**, **power reset**.»

Quindi:
1. **Non è corruzione e non serve il reset di fabbrica**: i tre rimedi ufficiali sono runtime restart / reboot / power cycle (30 secondi, non un reset).
2. Il caso "restart richiesto da una app che non parte bene" è un **difetto noto della piattaforma**, non del tuo codice.
3. LED **RDY lampeggiante** + ping OK + 502 = nginx/WBM su, backend firmware no. Coerente al 100%.

Nota collegata: il **System Watchdog** (fw ≥ 2020.0) può scattare se il processo `plcnext` "behaves unexpectedly" o impiega troppo a chiudersi dopo uno stop/restart, e in quel caso il controller riparte da solo.

## Aggiornamento ipotesi

- **H1, H2 (nella forma "causa del brick")**: refutate. Restano difetti da correggere (il componente non è installato: requisito retain non soddisfatto).
- **H6 (safety / IO / hash)**: **declassata**: con un reboot reale lo stesso contenuto di progetto parte correttamente. Non è il contenuto.
- **H7 (nuova, primaria) — `RestartDevice()` non equivale a un reboot pulito**: il "restart of the firmware" richiesto dall'app è la transizione fragile; il cambio di progetto ha bisogno del ciclo completo del sistema operativo (ACF riletto da zero, tutti i servizi riavviati, `/opt/plcnext/shadowing` e i `.so` rimappati). Nel verso **template → utente** la differenza è massima perché si **aggiunge** un include ACF (libreria `libTriggerRetain.so` da caricare) mentre il runtime precedente aveva ancora mappato il progetto template: è il caso che sporadicamente non arriva a ready.
- **H7b — concausa: manca `sync`**: `ln -sfn` + `RestartDevice()` entro ~1 s (`main.py:82-88`). Su un riavvio immediato la nuova configurazione può non essere persistita/visibile. Un reboot manuale, fatto dopo, la trova corretta → spiega perché "il reboot fa funzionare tutto".

## Come gestirlo (risposta operativa)

Regola: **il cambio di progetto si conclude sempre con un riavvio di sistema reale, mai con `RestartDevice()`.**

1. **Cambiare il tipo di riavvio.** Opzioni nel container (in ordine di preferenza):
   - `busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager Reboot b true` (il socket dbus è già montato nella quadlet) oppure `systemctl reboot` / `/sbin/reboot`;
   - se il container non ha i permessi: **discrepanza da correggere** — `docker-compose.yml` ha `privileged: true`, mentre la quadlet installata (`axis-control.container`) **no**. Va aggiunto ciò che serve (`AddCapability=CAP_SYS_BOOT`, o privileged) oppure la quadlet non è quella che gira.
   - fallback affidabile: `/etc/init.d/plcnext restart` (rimedio ufficiale, non richiede reboot del sistema).
2. **`sync` + verifica prima del riavvio**: scrivere i file, `sync`, verificare che il symlink e gli include ACF siano quelli attesi, **poi** riavviare.
3. **Watchdog di completamento** (obbligatorio, perché il container muore col reboot): file di stato `/opt/plcnext/user/switch.state` con `step` + `deadline`; uno script sull'host (`admin` crontab `@reboot` + `* * * * *`, non c'è systemd per il runtime) che, se lo switch è `pending` oltre N secondi o se il firmware non è `Running`, forza `/etc/init.d/plcnext restart` e poi `reboot`. Così un boot fallito si auto-recupera e **non serve mai più il reset di fabbrica**.
4. **Non lasciare il container come unico attore**: la sequenza critica (symlink, include ACF, trigger retain, riavvio) va in uno script sull'host invocato come oneshot, perché `RestartDevice()` termina il container a metà sequenza (`_do_project_switch`, `main.py:150-175`, non gira mai).
5. **Diagnostica**: log in `/opt/plcnext/logs/` (non `journalctl`, che è vuoto perché `plcnext` parte da SysV init). Scrivere sempre progetto attivo, step e esito.

## Impatto sul requisito "retain obbligatorio"

Con un **reboot reale** il flusso retain ha finalmente senso: il FW fa il backup automatico ad ogni avvio, e `TriggerRetain` può consumare il suo trigger. Ma oggi:
- `/opt/plcnext/projects/TriggerRetain/` **non esiste** e `Default/TriggerRetain.acf.config` **non c'è** → componente mai caricato;
- `/opt/plcnext/retaining/backups` **vuota** → nessun backup mai creato;
- `BackupAction.txt = restore` resta **inerte**.

→ Va prima **installato** il componente (cartella `TriggerRetain/` con `lib/libTriggerRetain.so` + il suo `.acf.config`), poi reso **project-aware** (backup per progetto, ripristino solo se il `.prb` corrisponde al progetto di destinazione, fallback a `Start(Warm)`), altrimenti il rischio `LayoutMismatch`/`CrcFileMismatch` torna quando i backup esisteranno davvero.
