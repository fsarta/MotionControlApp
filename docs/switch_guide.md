# Procedura completa — cambio progetto utente ↔ template, senza più brick

**Controller**: Yaskawa iC9226M-FSoE (PLCnext), FW 2026.6
**App**: `AxisControlPanel` (container `axis-control-panel`, Flask + gRPC)
**Stato di partenza**: codice modificato nel repo, **niente ancora provato sul controller**.

---

## 0. Riepilogo: cosa abbiamo scoperto (e perché la procedura è questa)

| # | Fatto accertato | Conseguenza |
|---|---|---|
| 1 | Con un **reboot manuale** lo switch template → utente funziona sempre | il codice di switch era corretto: il problema è **come** viene chiesto il riavvio |
| 2 | L'app usa `RestartDevice()` (gRPC) | Phoenix documenta che il *"restart of the firmware requested by an app"* sporadicamente non arriva a ready → **WBM 502**, LED **RDY** lampeggiante |
| 3 | `CanReboot` = **yes**, `sudo -n systemctl restart plcnext` e `sudo -n reboot` funzionano dal container | possiamo fare un **reboot di sistema** vero, che è ciò che serve |
| 4 | `/etc/init.d/plcnext` **non esiste** | da FW 2022.0 il runtime è il servizio **systemd `plcnext`** |
| 5 | La catena include è: `/etc/plcnext/device/Default.acf.config` → `$ARP_PATH_PROJECT_DEFAULT_DIR$/Default.acf.config` → **`<Include path="*.acf.config"/>` in `/opt/plcnext/projects/Default/`** | il punto di registrazione corretto è **`/opt/plcnext/projects/Default/`** (in `/etc/plcnext/device/` gli include prendono **solo** file di sistema) |
| 6 | `/opt/plcnext/projects/Default/TriggerRetain.acf.config` non c'era, `backups/` vuota, `restore` inerte | il componente **non è mai stato caricato**: per questo il retain non veniva conservato |

**Le tre correzioni**: (a) riavvio **di sistema** invece di `RestartDevice()`; (b) componente ACF registrato **dove il firmware lo legge**; (c) watchdog sull'host che recupera se il boot non completa.

---

## 1. Cosa è già stato modificato nel repo (e cosa fa)

| File | Cosa contiene |
|---|---|
| `AxisControlPanel/project_switch.py` | **nuovo**. Tutta la logica di switch: staging + verifica + swap atomico, reboot di sistema, stato su disco, registrazione ACF, scoperta del progetto utente |
| `AxisControlPanel/main.py` | `setup_parallel_project()` / `teardown_parallel_project()` ora delegano a `project_switch`; niente più `RestartDevice()`; niente teardown su SIGTERM; registrazione ACF all'avvio |
| `AxisControlPanel/host/switch-watchdog.sh` | **nuovo**. Gira sull'host: se lo switch resta "pending" e il firmware non è su, fa restart del runtime e poi reboot |
| `AxisControlPanel/host/collect-acf-info.sh` | **nuovo**. Diagnostica di sola lettura: dove sta la config ACF, cosa contiene, cosa dice il log |
| `docs/switch_guide.md`, `docs/switch_bug_analysis.md` | questa guida e l'analisi tecnica |

Punti precisi del nuovo comportamento:

- **Reboot**: `busctl` → `systemctl reboot` → `/sbin/reboot -f`, fallback `systemctl restart plcnext`. Mai `RestartDevice()`.
- **Prima del riavvio**: `sync` + `fsync` dello stato; verifica che ogni `binaryPath` degli `*.acf.config` esista; niente file cancellati senza controllo.
- **Swap**: copia in `<progetto>.staging`, verifica, `mv`, poi `ln -sfn`; il symlink viene ricontrollato con `realpath`.
- **ACF**: libreria in `/opt/plcnext/projects/TriggerRetain/lib/libTriggerRetain.so`, registrazione in **un solo punto** (`/opt/plcnext/projects/Default/TriggerRetain.acf.config`, letto dalla catena di include), `binaryPath="$ARP_PROJECTS_DIR$/TriggerRetain/lib/libTriggerRetain.so"` **assoluto** (il `./lib/...` del repo, letto da `Default/`, risolverebbe in `Default/lib/...`). Il file in `<progetto>/Libs/TriggerRetain/` **non** si tocca: non è nel percorso di include ed è referenziato dal meta.
- **Fine switch**: `finish_switch()` chiede il reboot; se il processo sopravvive (reboot rifiutato) fa il restart del runtime e verifica con `systemctl show -p ActiveEnterTimestamp`.
- **Nessun teardown implicito**: un SIGTERM (stop app dal WBM, aggiornamento) non riporta più il progetto a `PCWE`.

---

## FASE 1 — Diagnostica (sola lettura, 2 minuti)  ⟵ *sei qui*

Serve a bloccare **dove** registrare il componente prima di toccare qualsiasi cosa.

```bash
scp AxisControlPanel/host/collect-acf-info.sh admin@<IP>:/tmp/
ssh admin@<IP> 'sudo /tmp/collect-acf-info.sh; cat /tmp/acf-info.txt'
```

Cosa guardare nell'output:

1. `cat /etc/plcnext/device/Default.acf.config` → include `*.acf.config` (sistema), `$ARP_PATH_APPS_ACF_CONFIGS_DIR$` (app), `$ARP_PATH_PROJECT_DEFAULT_DIR$/Default.acf.config` (progetto);
2. `cat /opt/plcnext/projects/Default/Default.acf.config` → include `*.acf.config` **e** `$ARP_PATH_PROJECT_CURRENT_DIR$/*.acf.config` ⟹ è **qui** che va messa la registrazione;
3. `grep -rl TriggerRetain /etc/plcnext /opt/plcnext/projects` → conferma che non è mai stato caricato (nessuna traccia in `Arp.log`);
4. `tail -200 /opt/plcnext/logs/Arp.log | grep -iE "acf|retain"` → i log utili sono in `/opt/plcnext/logs/Arp.log` (il `journalctl` filtrato mostrava solo warning di shutdown).

### Esito reale (19/11, FW 2026.6) — già verificato

- `/opt/plcnext/projects/Default/TriggerRetain.acf.config` **esiste** ma non è mai stato eseguito: **manca `/opt/plcnext/projects/TriggerRetain/`** (né `lib/libTriggerRetain.so` né `TriggerRetainLibrary.acf.config`), quindi l'`Include` non risolve nulla. **Questo è il motivo per cui il retain non ha mai funzionato.**
- `readlink -f current` → **`/opt/plcnext/projects/PCWE`** (progetto utente attivo), `BackupAction.txt` = `restore` non consumato, `backups/` vuota.

⟹ **Non serve altra diagnostica: si può passare alla Fase 2.**

---

## FASE 2 — Rebuild e deploy dell'immagine

Dalla tua macchina Windows, nella cartella `AxisControlPanel`:

```bash
docker buildx build --platform linux/arm/v7 -t axis-control-panel:latest --no-cache --load .
docker save axis-control-panel:latest -o images\axis-control-panel.tar
docker run --rm -v "%cd%/images:/data" docker.io/library/alpine:latest gzip /data/axis-control-panel.tar
docker images --no-trunc --format "{{.ID}}" axis-control-panel:latest     # hash per app_info.json
scp images\axis-control-panel.tar.gz admin@<IP>:/tmp/
```

Sul controller:

```bash
podman load -i /tmp/axis-control-panel.tar.gz
```

Poi aggiorna l'hash dell'immagine in `app_info.json` e in `axis-control.container`, e reinstalla l'app dal WBM (o riavvia il container, vedi Fase 6).

**Verifica**: `podman images | grep axis-control` mostra la nuova immagine.

---

## FASE 2-bis (opzionale, consigliata) — Provare il componente retain SENZA rebuild

Permette di validare in 5 minuti la registrazione ACF, senza ricostruire l'immagine.

```bash
# 1. copia il bundle presente nel repo (TriggerRetain/lib + config) sul controller
scp -r AxisControlPanel/TriggerRetain admin@<IP>:/tmp/
scp AxisControlPanel/host/install-retain-component.sh admin@<IP>:/tmp/

# 2. installa e verifica
ssh admin@<IP>
sudo /tmp/install-retain-component.sh /tmp/TriggerRetain
```

Lo script crea `TriggerRetain/lib/libTriggerRetain.so` + `TriggerRetainLibrary.acf.config`,
scrive `Default/TriggerRetain.acf.config`, riavvia il runtime e stampa:

- `systemctl is-active plcnext`
- le righe con `TriggerRetain` in `/opt/plcnext/logs/*.log`
- il contenuto di `/opt/plcnext/retaining/backups/`

**Esito atteso**: il componente compare nei log; i backup possono restare vuoti
finché il progetto utente non ha variabili `RETAIN` (`NoRetainData`).

**Se compare un errore di caricamento** (`SharedLibrary ... not destroyed`,
`No appenders could be found`) → mi mandi `Arp.log` e correggo prima del rebuild.

Rollback di questa prova (torna allo stato attuale, che è già quello "senza componente"):

```bash
sudo rm -f /opt/plcnext/projects/Default/TriggerRetain.acf.config
sudo rm -rf /opt/plcnext/projects/TriggerRetain
sudo systemctl restart plcnext
```

## FASE 3 — Installare il watchdog sull'host (2 minuti)

```bash
scp AxisControlPanel/host/switch-watchdog.sh admin@<IP>:/tmp/
ssh admin@<IP>
sudo cp /tmp/switch-watchdog.sh /opt/plcnext/user/switch-watchdog.sh
sudo chmod +x /opt/plcnext/user/switch-watchdog.sh
( crontab -l 2>/dev/null | grep -v switch-watchdog ; \
  echo '@reboot sleep 60 && /opt/plcnext/user/switch-watchdog.sh' ; \
  echo '* * * * * /opt/plcnext/user/switch-watchdog.sh' ) | crontab -
crontab -l
```

**Come funziona**: legge `/opt/plcnext/user/switch.state` (lo scrive l'app). Se la fase è `pending` e la deadline è passata e il firmware non risponde (`http://127.0.0.1/wbm/` non risponde e `Arp.System.Application` non c'è), fa `systemctl restart plcnext`; se basta, marca `done`; altrimenti `reboot`. Log in `/opt/plcnext/logs/switch-watchdog.log`.

**Verifica**: `echo '{}' > /opt/plcnext/user/switch.state; /opt/plcnext/user/switch-watchdog.sh; echo $?` → deve uscire `0` senza fare nulla.

---

## FASE 4 — Verificare che il componente retain venga caricato

Avvia l'app dal WBM, poi:

```bash
# 1. i file devono esserci
sudo ls -la /opt/plcnext/projects/TriggerRetain/lib/
sudo ls -la /opt/plcnext/projects/Default/TriggerRetain.acf.config
sudo cat /opt/plcnext/projects/TriggerRetain/TriggerRetainLibrary.acf.config

# 2. il runtime deve averlo caricato
sudo systemctl restart plcnext && sleep 45
journalctl -u plcnext -b --no-pager | grep -i -E "triggerretain|retain|backup" | tail -30

# 3. dopo un po' di funzionamento devono comparire i backup
sudo ls -la /opt/plcnext/retaining/backups/
```

Esiti possibili:

- **compare `TriggerRetain` nei log e `backups/` si popola** → retain funzionante, si passa alla Fase 5;
- **`backups/` resta vuota ma il componente è nei log**: probabile `BackupError=NoRetainData` (= il progetto utente non ha variabili `RETAIN`). In quel caso il retain "di sistema" non ha nulla da salvare e serve la persistenza applicativa via gRPC (te la preparo: lista di variabili → JSON in `/opt/plcnext/user/` → rilettura dopo lo switch);
- **nessuna traccia del componente**: mi mandi `journalctl -u plcnext -b | tail -100`, il percorso di registrazione va corretto.

---

## FASE 5 — La prova dei due switch

1. Dall'app: **utente → template**. Ti compare il messaggio di conferma, il controller si riavvia **da solo**.
2. Attendi il ritorno del WBM (60–90 s) e verifica che l'app risponda.
3. Prova gli assi: devono funzionare come prima (questa direzione già funzionava).
4. Dall'app: **template → utente**. È il caso che si rompeva.
5. Attendi 2–3 minuti **senza toccare nulla**. Se il firmware non torna su, il watchdog interviene da solo.

Cosa controllare in ogni momento:

```bash
cat /opt/plcnext/user/switch.state          # fase, step, deadline
cat /opt/plcnext/logs/switch-watchdog.log   # interventi del watchdog
readlink -f /opt/plcnext/projects/current   # deve puntare al progetto atteso
systemctl is-active plcnext                 # active
```

---

## FASE 6 — Recupero manuale (in quest'ordine, MAI reset di fabbrica)

```bash
sudo systemctl restart plcnext     # 30–45 s, rimedio documentato, non tocca i progetti
systemctl is-active plcnext
# se ancora giu':
sudo reboot
```

Il reset di fabbrica **non serve più**: non c'era corruzione, e con `projects-good.tgz` hai comunque il backup dello stato buono in `/root/`.

---

## FASE 7 — Se qualcosa non torna: cosa raccogliere

```bash
sudo tar czf /tmp/logs-fault.tgz /opt/plcnext/logs /opt/plcnext/user \
    /opt/plcnext/retaining /etc/plcnext/device /opt/plcnext/projects/Default
journalctl -u plcnext -b --no-pager | tail -200
cat /opt/plcnext/user/switch.state
ps aux | grep -i -E "arp|plcnext|nginx" | head -20
```

---

## FASE 8 — Cose da sistemare dopo che lo switch funziona

1. **`privileged: true` vs quadlet**: `docker-compose.yml` lo ha, `axis-control.container` no. Con `CanReboot=yes` e `sudo -n` funzionante non è bloccante, ma va allineato per coerenza.
2. **`Libs/TriggerRetain/` nel template**: contiene un `TriggerRetainLibrary.acf.config` che punta a `./lib/libTriggerRetain.so` inesistente. Ora `project_switch` lo rimuove durante lo staging; meglio toglierlo anche dal template.
3. **`pylibs/`**: non è versionato, quindi un build dal repo fallisce (`COPY pylibs/pxc_grpc`). Va aggiunto un `.gitignore` mirato o scaricato in build.
4. **`discover_axes()`** (`grpc_client.py`): path `/opt/plcnext/projects/PCWE/...` hardcoded → usare il symlink `current`.
5. **Retain project-aware nel C++**: prima di fidarsi del ripristino tra progetti diversi va aggiunto il controllo che il `.prb` corrisponda al progetto di destinazione, con fallback a `Start(Warm)`. Da fare **dopo** aver visto almeno un backup reale.

---

## Ordine di esecuzione (riassunto)

```
FASE 1  diagnostica           (FATTA, 19/11)
FASE 2  rebuild + deploy      (~30 min)
FASE 2-bis prova componente   (5 min, opzionale ma consigliata)
FASE 3  watchdog sull'host    (2 min)
FASE 4  verifica componente   (5 min)
FASE 5  prova dei due switch  (10 min)
FASE 6  recupero, se serve    (2 min)
FASE 7  raccolta log, se serve
FASE 8  pulizie successive
```
