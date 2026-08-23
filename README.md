# VPN Benchmark — NordVPN vs ProtonVPN

Stack Docker qui compare deux fournisseurs WireGuard **au niveau du réseau, pas
d'un seul serveur** : plusieurs serveurs, plusieurs pays, plusieurs rounds
étalés dans le temps, avec une mesure sans VPN (baseline) à chaque round.

Conçue pour être déployée **depuis Portainer, sans aucun accès shell au NAS** :
l'image est publiée sur GHCR par GitHub Actions, Portainer n'a qu'à la tirer.
Un seul conteneur est déclaré ; il pilote lui-même les conteneurs gluetun,
qBittorrent et les sondes via le socket Docker, puis publie le rapport sur un
port HTTP.

La sonde de mesure partage la pile réseau du conteneur gluetun : **ce que tu
mesures est exactement ce que verra ton conteneur applicatif en production**.

---

## 1. Déploiement dans Portainer

L'image est publiée automatiquement sur **GitHub Container Registry** par
GitHub Actions à chaque push sur `main` :

```
ghcr.io/hitman47/vpn-benchmark:latest
```

Rien n'est construit sur le NAS, rien n'est à construire sur ton PC.

1. Portainer → **Stacks** → **Add stack** → onglet **Web editor**
2. Colle le contenu de [`docker-compose.yml`](docker-compose.yml)
3. Dans **Environment variables**, ajoute au minimum :

   | Nom | Valeur |
   |---|---|
   | `NORD_WIREGUARD_PRIVATE_KEY` | ta clé NordLynx |
   | `PROTON_WIREGUARD_PRIVATE_KEY` | ta clé Proton |
   | `LAN_SUBNET` | ton sous-réseau, ex. `192.168.1.0/24` |
   | `BENCH_MODE` | `smoke` pour le premier run |

4. **Deploy the stack** — Portainer tire l'image et démarre la campagne.
5. Suis l'avancement dans les logs du conteneur `vpnbench-orchestrator`.

Variante *Repository* (Portainer suit le dépôt Git) : URL du dépôt, compose
path `docker-compose.yml`, **Pull latest image activé**. Le `pull_policy: always`
du compose garantit que la dernière image publiée est récupérée à chaque
redéploiement.

### Mettre à jour l'image

Chaque push sur `main` déclenche le workflow
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) qui republie
`:latest` et un tag `:sha-xxxxxxx`. Côté Portainer : *Update the stack* →
*Re-pull image and redeploy*.

### Construire en local (développement)

Pour tester une modification sans passer par GHCR :

```bash
docker compose -f docker-compose.build.yml up --build
```

`publish.ps1` / `publish.sh` restent disponibles pour publier une image à la
main vers un autre registre, mais ne sont plus nécessaires au fonctionnement
normal.

---

## 2. Lire les résultats

Le conteneur **reste actif après la campagne** et sert le rapport :

```
http://<ip-du-nas>:8888/
```

Tu y trouves `report.html` (verdict, tableaux, graphiques), `measurements.csv`,
`cases.csv`, `results.db` (SQLite) et `gluetun-winner.docker-compose.yml` — la
config gluetun prête à l'emploi du provider gagnant.

Le résumé comparatif est aussi imprimé dans les **logs du conteneur**, en clair.

### Tester plusieurs pays en une passe

```bash
BENCH_MODE=multi
```

Les **deux** providers sont mesurés sur exactement la même liste de pays
(`Netherlands, Switzerland, Sweden, France` par défaut, modifiable par
`BENCH_COUNTRIES`), avec toute la batterie de tests et une baseline. Compter
~15 min par pays et par provider, soit ~2 h pour quatre pays.

Le test torrent, lui, ne tourne que sur le **premier** pays
(`BENCH_P2P_MAX_COUNTRIES`) : il coûte deux fois `BENCH_P2P_MINUTES` chez Proton
à cause du bras témoin sans port forwarding, et le swarm est le même partout —
le rejouer dans chaque pays allongerait la campagne sans rien apprendre. Mets
`BENCH_P2P_MAX_COUNTRIES=0` si tu veux quand même le torrent partout.

Un pays où un seul des deux providers arrive à se connecter est **exclu du
score** et signalé comme tel dans le rapport : lance `BENCH_MODE=preflight`
avant si tu veux savoir à l'avance lesquels tiendront.

### Où atterrissent les téléchargements du test P2P

Par défaut dans un volume Docker, donc sur le **disque système** du NAS. Le test
torrent peut y écrire plusieurs Go (plafonné par `BENCH_MAX_DOWNLOAD_GB`, purgé
après chaque cas). Pour l'envoyer ailleurs :

```bash
BENCH_DOWNLOADS_PATH=/mnt/nvme/vpnbench-downloads
```

Le répertoire doit **exister** sur l'hôte et être inscriptible avant le
déploiement, sinon Docker refuse de démarrer le conteneur. L'orchestrateur écrit
au démarrage la destination retenue et l'espace libre :

```
telechargements : /mnt/nvme/vpnbench-downloads (chemin de l'hote) - 812.4 Go libres
```

### Vérifier d'abord que les deux clés couvrent les mêmes pays

```
BENCH_MODE=preflight
```

~2 min, aucune mesure : le banc tente une connexion par provider et par pays,
affiche l'IP de sortie obtenue et conclut par la liste des **pays comparables**
— ceux où les deux providers répondent. Résultat conservé dans
`http://<ip-du-nas>:8888/preflight.txt`.

À propos de ProtonVPN : la clé privée WireGuard est liée à **ton compte**, pas à
un serveur. Le fichier téléchargé depuis le portail contient l'`Endpoint` et la
`PublicKey` d'un serveur précis, mais gluetun remplace ces deux lignes par
celles du serveur qu'il choisit — c'est ce qui lui permet de couvrir tout le
parc avec une seule clé. Ce qui est bien figé à la génération, ce sont les
options : **coche P2P et NAT-PMP**, sinon le port forwarding ne sera pas testé.

Si malgré tout un pays ne répond que chez un seul provider, le banc l'exclut du
score plutôt que de comparer deux pays différents (voir §5).

### Quand gluetun refuse la configuration

Une valeur d'environnement invalide fait sortir gluetun en une seconde, et donc
échouer *tous* les cas. Le motif réel est maintenant remonté en tête du
diagnostic plutôt que noyé sous la bannière :

```
CONFIGURATION REFUSEE PAR GLUETUN : log level: level is not recognized: multi
  -> corrige la variable d'environnement de la stack, aucun repli ne peut compenser
```

Dans ce cas le banc ne perd plus de temps en second essai — aucun changement de
serveur ne corrige une valeur invalide — et si les **deux** providers échouent
pour ce motif, la campagne s'arrête immédiatement au lieu de dérouler la matrice
à vide.

### Quand un tunnel refuse de monter

Chaque échec de connexion est écrit en entier dans `results/failures/`, donc
consultable sans shell :

```
http://<ip-du-nas>:8888/failures/
```

Le fichier contient l'état du conteneur gluetun, son code de sortie et
**l'intégralité de ses logs**, pour les deux tentatives (serveur épinglé, puis
repli sur le pays seul). Les mêmes messages apparaissent dans la section
*Incidents* du rapport HTML. Pour aller plus loin :
`BENCH_GLUETUN_LOG_LEVEL=debug`.

gluetun n'a pas à figurer dans la stack : l'orchestrateur crée et détruit
lui-même un conteneur `vpnbench-vpn` pour chaque serveur testé.

### Enchaîner les campagnes

| Étape | Action dans Portainer |
|---|---|
| Vérifier les clés ~2 min | `BENCH_MODE=preflight` → Deploy |
| Validation ~20 min | `BENCH_MODE=smoke` → Deploy |
| Tous les pays d'un coup ~2 h | `BENCH_MODE=multi` → Update the stack |
| Campagne ~24 h | `BENCH_MODE=full` → Update the stack (re-deploy) |
| Repondérer le verdict sans remesurer | `BENCH_MODE=report` → Update the stack |

Chaque redéploiement démarre une nouvelle campagne ; l'historique reste dans la
base SQLite du volume `results`.

---

## 3. Variables d'environnement

Une seule chose est obligatoire : les deux clés WireGuard. **Toute variable
laissée vide reprend la valeur de `bench.yaml` pour le mode choisi** — il n'y a
donc rien d'autre à remplir pour démarrer.

### Obligatoires

| Variable | Effet |
|---|---|
| `NORD_WIREGUARD_PRIVATE_KEY` | clé privée NordLynx |
| `PROTON_WIREGUARD_PRIVATE_KEY` | clé privée ProtonVPN |

### Environnement

| Variable | Défaut | Effet |
|---|---|---|
| `LAN_SUBNET` | `192.168.1.0/24` | autorise l'accès local à travers le pare-feu gluetun |
| `TZ` | `Europe/Paris` | fuseau horaire des logs et du rapport |
| `PROTON_WIREGUARD_ADDRESSES` | `10.2.0.2/32` | adresse fournie avec la config Proton |
| `GLUETUN_IMAGE` | `qmcgaw/gluetun:v3.40` | version de gluetun évaluée |
| `BENCH_GLUETUN_LOG_LEVEL` | `info` | niveau de log de gluetun : `debug`, `info`, `warning`, `error` **et rien d'autre** — une valeur inconnue faisait mourir tous les tunnels, elle est désormais refusée et ignorée. Le mode de campagne, lui, se choisit avec `BENCH_MODE` |
| `BENCH_IMAGE` | image GHCR | pour épingler un tag précis, ex. `…:sha-c8e7851` |
| `BENCH_WEB_PORT` | `8888` | port du rapport **côté NAS** (mapping du compose) |

### Campagne

| Variable | Défaut smoke / full | Effet |
|---|---|---|
| `BENCH_MODE` | `smoke` | `preflight` / `smoke` / `multi` / `full` / `report` |
| `BENCH_COUNTRIES` | `Netherlands` / `Netherlands,Switzerland,Sweden` | pays de sortie testés |
| `BENCH_ROUNDS` | `1` / `24` | nombre de passes |
| `BENCH_INTERVAL_MINUTES` | `0` / `60` | espacement entre deux débuts de passe |
| `BENCH_SERVERS_PER_COUNTRY` | `1` / `2` | serveurs testés par pays |
| `BENCH_INCLUDE_BASELINE` | `true` | mesure sans VPN à chaque round |

### P2P

| Variable | Défaut smoke / full | Effet |
|---|---|---|
| `BENCH_P2P_ENABLED` | `true` | active le test torrent réel |
| `BENCH_P2P_MINUTES` | `3` / `8` | durée d'un test torrent |
| `BENCH_PF_AB` | `true` | rejoue le test torrent **sans** port forwarding sur le même serveur ProtonVPN, pour isoler l'apport du NAT-PMP (double la durée P2P) |
| `BENCH_P2P_MAX_COUNTRIES` | `1` | nombre de pays où le test torrent est joué (les premiers de `BENCH_COUNTRIES`) ; `0` = tous |
| `BENCH_DOWNLOADS_PATH` | *(vide)* | chemin **de l'hôte** où écrire les téléchargements P2P, par ex. un NVMe dédié. Vide = volume Docker, donc disque système du NAS |
| `BENCH_P2P_EVERY_ROUNDS` | `1` / `4` | fréquence du test torrent |
| `BENCH_MAX_DOWNLOAD_GB` | `4` | garde-fou sur le volume téléchargé |

### Réglage fin des mesures

| Variable | Défaut smoke / full | Effet |
|---|---|---|
| `BENCH_THROUGHPUT_SECONDS` | `8` / `12` | durée d'un test de débit |
| `BENCH_THROUGHPUT_STREAMS` | `8` | flux parallèles en descendant (moitié en montant) |
| `BENCH_LATENCY_COUNT` | `15` / `30` | pings par cible |
| `BENCH_WEB_REPEATS` | `2` / `3` | répétitions par URL |
| `BENCH_KILLSWITCH` | `true` | coupe le tunnel à chaud pour tester l'étanchéité |

### Cibles (listes séparées par des virgules)

| Variable | Défaut | Effet |
|---|---|---|
| `BENCH_PING_TARGETS` | `1.1.1.1,8.8.8.8,9.9.9.9` | cibles de latence |
| `BENCH_WEB_URLS` | 4 sites, voir `bench.yaml` | pages mesurées en TTFB |
| `BENCH_DOWNLOAD_URLS` | 5 cibles, voir `bench.yaml` | candidates pour la mesure de débit |
| `BENCH_TORRENTS` | ISO Ubuntu 24.04 | `.torrent` utilisés pour le test P2P |

### Interne (à ne changer qu'en connaissance de cause)

| Variable | Défaut | Effet |
|---|---|---|
| `BENCH_HTTP_PORT` | `8888` | port d'écoute **dans** le conteneur |
| `BENCH_KEEP_ALIVE` | `true` | `false` = le conteneur s'arrête à la fin du run |
| `BENCH_YAML` | — | chemin d'un `bench.yaml` personnalisé |
| `BENCH_RESULTS` | `/app/results` | dossier des résultats dans le conteneur |
| `BENCH_SELF_NAME` | `vpnbench-orchestrator` | nom du conteneur, sert à l'auto-inspection |
| `BENCH_PROJECT` | `vpnbench` | préfixe des conteneurs créés par l'orchestrateur |

### Aller plus loin que les variables

Les poids du score, les serveurs iperf3 et le détail des modes vivent dans
`bench.yaml`, embarqué dans l'image. Pour le surcharger sans reconstruire :
décommente le volume `config:/config` dans le compose et dépose ton propre
`bench.yaml` dedans (l'ordre de recherche est `BENCH_YAML`, puis
`/config/bench.yaml`, puis celui de l'image).

---

## 4. Ce qui est mesuré

**Débit** — multi-flux (8 descendants, 4 montants) sur fenêtre bornée. La cible
de téléchargement est **choisie automatiquement au démarrage** parmi une liste
de gros fichiers statiques (OVH, Hetzner, Tele2, Cloudflare) : la plus rapide
joignable est retenue et **gardée pour tous les cas de la campagne**, sinon la
comparaison entre providers ne voudrait rien dire. Si la cible se met à répondre
429/403, c'est signalé dans les logs et le rapport. Montée via Cloudflare `__up`.
iperf3 en option dans `bench.yaml`.

**Latence** — fping sur 3 cibles : RTT moyen, min, p95, gigue, perte.

**Bufferbloat** — latence à vide vs latence pendant saturation du lien.

**Web réel** — DNS, TCP, TLS, TTFB et temps total sur 4 sites.

**P2P / torrent** — qBittorrent réel derrière le tunnel, torrent légal (ISO
Ubuntu), swarm réel : débit, seeds vus, délai avant le premier peer, et surtout
**peers entrants**.

**Port forwarding** — NAT-PMP demandé via gluetun, puis vérification que le port
est *réellement* joignable depuis l'extérieur, une fois qBittorrent en écoute.
Proton en dispose, Nord non : c'est généralement le facteur qui décide pour un
usage torrent.

**Intérêt du port forwarding (test A/B)** — le port forwarding est aussi testé
*contre lui-même*. Juste après le cas ProtonVPN normal, le même serveur est
rouvert avec `VPN_PORT_FORWARDING=off` et le même torrent est rejoué. Comparer
Proton-avec-PF à Nord-sans-PF mesurerait deux opérateurs à la fois ; comparer
Proton-avec-PF à Proton-sans-PF isole la redirection de port et elle seule. Le
rapport en tire une section dédiée : peers entrants, débit descendant et
montant, délai avant le premier peer, avec et sans. Désactivable par
`BENCH_PF_AB=false`.

**Sécurité** — fuite IPv6, fuite DNS (ASN du résolveur comparé à celui de ta
connexion nue), IP de sortie identique à l'IP nue, MTU du chemin, et kill-switch
(le tunnel est coupé à chaud, le trafic doit mourir avec lui).

**Fiabilité** — temps de connexion, taux de connexions réussies, variance round
après round, et CPU du conteneur VPN — pour détecter le cas où **ton NAS** est
le facteur limitant, pas le VPN.

---

## 5. Méthode (pourquoi les chiffres sont comparables)

1. **Ordre inversé à chaque round** : round pair Nord→Proton, round impair
   Proton→Nord. La dérive du réseau au fil de la journée ne favorise personne.
2. **Baseline à chaque round** : la connexion sans VPN est remesurée en continu,
   d'où des résultats en *% de débit conservé* plutôt qu'en valeur absolue.
3. **Jamais en parallèle** : les deux providers ne sont jamais testés en même
   temps, sinon ils se voleraient la bande passante.
4. **Médianes, pas moyennes** : un pic de congestion ne fausse pas le résultat.
5. **Serveurs choisis par API** (charge la plus faible, groupe P2P) et épinglés
   par nom, donc le même serveur est retesté round après round.
6. **Le port forwarding est isolé** : son apport se mesure à provider et
   serveur constants (bras témoin NAT-PMP coupé, joué dans la foulée), jamais
   en comparant deux fournisseurs.
7. **Seuls les pays comparables comptent** : un pays où un seul des deux
   providers a réussi à se connecter est exclu du score, et signalé comme tel
   dans le rapport. Comparer NordVPN aux Pays-Bas à ProtonVPN en France
   mesurerait la distance, pas le fournisseur.

Le score est une somme pondérée de métriques normalisées : sur chaque métrique
le meilleur vaut 1,0 et l'autre une fraction proportionnelle. Modifie les poids
dans `bench.yaml`, puis relance en `BENCH_MODE=report` pour recalculer sans
remesurer.

---

## 6. Extraction des clés WireGuard

**ProtonVPN** — Compte → Downloads → WireGuard configuration. Coche *P2P* et
*NAT-PMP (port forwarding)*, choisis n'importe quel serveur : seule la ligne
`PrivateKey` compte, gluetun gère les endpoints. Reporte aussi `Address` dans
`PROTON_WIREGUARD_ADDRESSES` si elle diffère de `10.2.0.2/32`.

**NordVPN** — la clé NordLynx n'est pas exposée dans l'interface web. Depuis un
Linux avec le client officiel :

```bash
nordvpn set technology nordlynx && nordvpn connect && sudo wg show nordlynx private-key
```

Sinon, via l'API avec un token créé sur le dashboard Nord :

```bash
curl -s -u token:TON_TOKEN https://api.nordvpn.com/v1/users/services/credentials
```

Le champ `nordlynx_private_key` de la réponse est la clé.

Une seule clé par provider suffit pour joindre tous leurs serveurs.

---

## 7. Ce que la stack demande à l'hôte

- **`/var/run/docker.sock` monté** : indispensable, l'orchestrateur crée et
  détruit les conteneurs gluetun / sonde / qBittorrent. À savoir : cela donne au
  conteneur un contrôle équivalent root sur l'hôte Docker. Ne déploie cette
  stack que depuis un dépôt et une image que tu contrôles.
- `/dev/net/tun` présent sur l'hôte (standard sur ZimaOS).
- ~10 Go libres pour le test torrent (nettoyés à la fin).
- Les conteneurs créés sont préfixés `vpnbench-` et supprimés automatiquement.

**Pourquoi ZimaOS plutôt qu'un Synology** : Docker y est standard
(`NET_ADMIN`, `sysctl`, `/dev/net/tun` sans bidouille) et le CPU x86 tient le
gigabit en WireGuard. Sur un NAS ARM/Celeron, le chiffrement plafonne vers
150-300 Mb/s et **tous les débits mesurés seraient ceux du NAS**. Le banc
surveille le CPU du conteneur gluetun et lève un avertissement au-delà de 90 %.

---

## 8. Limites connues

- **WebRTC** n'est pas testable hors navigateur : non couvert.
- Le débit Cloudflare mesure un chemin CDN, pas un backbone brut. Ajoute des
  serveurs iperf3 dans `bench.yaml` pour compléter.
- Le test P2P dépend de la santé du swarm à l'instant T ; d'où sa répétition
  (1 round sur 4 en mode `full`).
- Les deux comptes doivent tolérer une connexion simultanée supplémentaire
  pendant la campagne.
- Le trafic torrent est réel : il consomme bande passante et quota.

---

## 9. Architecture

```
Dockerfile                    image unique (orchestrateur + sonde)
.github/workflows/publish.yml build + push vers GHCR a chaque push sur main
docker-compose.yml            stack Portainer, image tirée de GHCR
docker-compose.build.yml      variante développement, construction locale
publish.ps1 / publish.sh      publication manuelle vers un autre registre
bench.yaml                    pays, durées, cibles, torrents, poids du score
orchestrator/
  main.py                     boucle rounds -> baseline -> providers -> serveurs
                              + serveur HTTP du rapport
  dockerctl.py                auto-inspection, cycle de vie gluetun, sondes
  providers.py                choix des serveurs via les API Nord/Proton
  p2p.py                      qBittorrent réel + vérification du port forwarding
  db.py                       SQLite format long + export CSV
  scoring.py                  agrégation, normalisation, verdict
  report.py                   résumé terminal, HTML autonome, compose gagnant
probe/probe.py                sonde exécutée dans la pile réseau testée
```

L'orchestrateur s'auto-inspecte au démarrage (image, réseau, volumes) : aucun
chemin de l'hôte n'est codé en dur, la stack se déploie telle quelle. C'est pour
cela que le service doit garder `container_name: vpnbench-orchestrator`.
