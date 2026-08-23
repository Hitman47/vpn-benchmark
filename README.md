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

### Enchaîner les campagnes

| Étape | Action dans Portainer |
|---|---|
| Validation ~20 min | `BENCH_MODE=smoke` → Deploy |
| Campagne ~24 h | `BENCH_MODE=full` → Update the stack (re-deploy) |
| Repondérer le verdict sans remesurer | `BENCH_MODE=report` → Update the stack |

Chaque redéploiement démarre une nouvelle campagne ; l'historique reste dans la
base SQLite du volume `results`.

---

## 3. Variables d'environnement

Obligatoires : `NORD_WIREGUARD_PRIVATE_KEY`, `PROTON_WIREGUARD_PRIVATE_KEY`.

| Variable | Défaut | Effet |
|---|---|---|
| `BENCH_MODE` | `smoke` | `smoke` / `full` / `report` |
| `LAN_SUBNET` | `192.168.1.0/24` | autorise l'accès local à travers le pare-feu gluetun |
| `BENCH_WEB_PORT` | `8888` | port du rapport sur le NAS |
| `BENCH_COUNTRIES` | selon le mode | ex. `Netherlands,Switzerland,Sweden` |
| `BENCH_ROUNDS` | 1 (smoke) / 24 (full) | nombre de passes |
| `BENCH_INTERVAL_MINUTES` | 60 en `full` | espacement entre deux passes |
| `BENCH_SERVERS_PER_COUNTRY` | 1 / 2 | serveurs testés par pays |
| `BENCH_P2P_ENABLED` | `true` | active le test torrent réel |
| `BENCH_P2P_MINUTES` | 3 / 8 | durée d'un test torrent |
| `BENCH_P2P_EVERY_ROUNDS` | 1 / 4 | fréquence du test torrent |
| `BENCH_MAX_DOWNLOAD_GB` | 4 | garde-fou sur le volume téléchargé |
| `BENCH_KILLSWITCH` | `true` | coupe le tunnel à chaud pour tester l'étanchéité |
| `PROTON_WIREGUARD_ADDRESSES` | `10.2.0.2/32` | adresse fournie avec la config Proton |
| `GLUETUN_IMAGE` | `qmcgaw/gluetun:v3.40` | version de gluetun évaluée |

Tout le reste (cibles ping, sites web, torrents, **pondération du score**) est
dans `bench.yaml`, embarqué dans l'image. Pour le modifier sans reconstruire :
monte un volume sur `/config` et dépose-y ton propre `bench.yaml`.

---

## 4. Ce qui est mesuré

**Débit** — Cloudflare speed en multi-flux (8 descendants, 4 montants), débit
soutenu sur fenêtre bornée. iperf3 en option dans `bench.yaml`.

**Latence** — fping sur 3 cibles : RTT moyen, min, p95, gigue, perte.

**Bufferbloat** — latence à vide vs latence pendant saturation du lien.

**Web réel** — DNS, TCP, TLS, TTFB et temps total sur 4 sites.

**P2P / torrent** — qBittorrent réel derrière le tunnel, torrent légal (ISO
Ubuntu), swarm réel : débit, seeds vus, délai avant le premier peer, et surtout
**peers entrants**.

**Port forwarding** — NAT-PMP demandé via gluetun, puis vérification que le port
est *réellement* joignable depuis l'extérieur. Proton en dispose, Nord non :
c'est généralement le facteur qui décide pour un usage torrent.

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
