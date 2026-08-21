"""Test P2P reel : qBittorrent derriere le tunnel, torrent legal, swarm reel.

Ce module mesure ce qui differencie vraiment les deux providers pour le
torrent : le debit dans un swarm, mais surtout la capacite a recevoir des
connexions ENTRANTES (port forwarding NAT-PMP chez Proton, absent chez Nord).
"""
import os
import shutil
import socket
import time

import docker
import requests

QBT_CONF = """[LegalNotice]
Accepted=true

[AutoRun]
enabled=false

[BitTorrent]
Session\\DefaultSavePath=/downloads
Session\\Port={port}
Session\\DHTEnabled=true
Session\\PeXEnabled=true
Session\\LSDEnabled=true
Session\\Encryption=0
Session\\GlobalMaxRatio=0
Session\\QueueingSystemEnabled=false
Session\\UseAlternativeGlobalSpeedLimit=false
Session\\AnonymousModeEnabled=false

[Preferences]
Connection\\PortRangeMin={port}
Connection\\UPnP=false
Downloads\\SavePath=/downloads
General\\Locale=en
WebUI\\Address=*
WebUI\\Port=8080
WebUI\\LocalHostAuth=false
WebUI\\CSRFProtection=false
WebUI\\HostHeaderValidation=false
WebUI\\AuthSubnetWhitelistEnabled=true
WebUI\\AuthSubnetWhitelist=172.16.0.0/12, 192.168.0.0/16, 10.0.0.0/8
"""


def _empty_dir(path):
    """Vide un repertoire sans le supprimer (c'est un point de montage)."""
    try:
        os.makedirs(path, exist_ok=True)
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass
    except OSError:
        pass


class P2PTester:
    def __init__(self, runner, cfg, log):
        self.runner = runner
        self.cfg = cfg
        self.log = log
        self.client = runner.client

    # ------------------------------------------------------------------
    def _volumes(self):
        """Volumes partages decouverts au demarrage (aucun chemin de l'hote)."""
        conf = self.runner.volumes.get("/shared/qbt")
        dl = self.runner.volumes.get("/shared/downloads")
        return conf, dl

    def _prepare_config(self, port):
        """Reecrit la conf qBittorrent dans le volume partage."""
        base = "/shared/qbt"
        conf_dir = os.path.join(base, "qBittorrent")
        for stale in ("qBittorrent", "data"):
            shutil.rmtree(os.path.join(base, stale), ignore_errors=True)
        os.makedirs(conf_dir, exist_ok=True)
        conf = QBT_CONF.format(port=port)
        with open(os.path.join(conf_dir, "qBittorrent.conf"), "w",
                  encoding="utf-8") as f:
            f.write(conf)
        # certaines images rangent la conf un niveau plus bas
        nested = os.path.join(conf_dir, "config")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "qBittorrent.conf"), "w",
                  encoding="utf-8") as f:
            f.write(conf)
        _empty_dir("/shared/downloads")

    def _start_qbt(self, vpn_container, port):
        conf_vol, dl_vol = self._volumes()
        name = "%s-qbt" % self.cfg.project
        try:
            self.client.containers.get(name).remove(force=True)
        except docker.errors.APIError:
            pass
        self._prepare_config(port)
        return self.client.containers.run(
            self.cfg.p2p["qbittorrent_image"], name=name, detach=True,
            network_mode="container:%s" % vpn_container.name,
            environment={"PUID": "1000", "PGID": "1000", "TZ": self.cfg.tz,
                         "WEBUI_PORT": "8080"},
            volumes={conf_vol: {"bind": "/config", "mode": "rw"},
                     dl_vol: {"bind": "/downloads", "mode": "rw"}},
            labels={"vpnbench": "1"},
        )

    # ------------------------------------------------------------------
    def _api(self, base, path, method="GET", **kw):
        url = base + path
        fn = requests.post if method == "POST" else requests.get
        kw.setdefault("timeout", 15)
        return fn(url, **kw)

    def _wait_api(self, base, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = self._api(base, "/api/v2/app/version", timeout=5)
                if r.status_code == 200:
                    return r.text.strip()
            except requests.RequestException:
                pass
            time.sleep(3)
        return None

    # ------------------------------------------------------------------
    def run(self, provider, vpn_container, exit_ip, minutes):
        """Retourne un dict de metriques P2P. Ne leve jamais."""
        res = {"ok": False, "port_forward_ok": 0, "forwarded_port": None}
        conf_vol, dl_vol = self._volumes()
        if not conf_vol or not dl_vol:
            res["error"] = ("volumes /shared/qbt et /shared/downloads absents "
                            "de la stack : test P2P desactive")
            self.log("  %s" % res["error"])
            return res
        qbt = None
        try:
            port = self.runner.forwarded_port(vpn_container)
            if port:
                res["forwarded_port"] = port
                self.log("port forwarding obtenu : %s" % port)
            else:
                port = 6881
                self.log("pas de port forwarding (attendu chez NordVPN)")

            # Le port est-il reellement joignable depuis l'exterieur ?
            res["port_forward_ok"] = 1 if self._external_port_open(exit_ip, port) else 0

            qbt = self._start_qbt(vpn_container, port)
            base = "http://%s:8080" % self.runner._container_ip(vpn_container)
            version = self._wait_api(base)
            if not version:
                res["error"] = "API qBittorrent injoignable"
                return res
            res["qbt_version"] = version

            self._api(base, "/api/v2/app/setPreferences", "POST",
                      data={"json": '{"listen_port": %d, "upnp": false}' % port})

            torrents = self.cfg.p2p["torrents"]
            self._api(base, "/api/v2/torrents/add", "POST",
                      data={"urls": "\n".join(torrents),
                            "savepath": "/downloads", "paused": "false"})

            res.update(self._monitor(base, minutes))

            # le port est-il joignable maintenant que qBittorrent ecoute ?
            if not res["port_forward_ok"]:
                res["port_forward_ok"] = 1 if self._external_port_open(exit_ip, port) else 0

            self._api(base, "/api/v2/torrents/delete", "POST",
                      data={"hashes": "all", "deleteFiles": "true"})
            res["ok"] = True
        except Exception as e:
            res["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            if qbt is not None:
                try:
                    qbt.remove(force=True)
                except docker.errors.APIError:
                    pass
            _empty_dir("/shared/downloads")
        return res

    # ------------------------------------------------------------------
    def _monitor(self, base, minutes):
        deadline = time.time() + minutes * 60
        max_bytes = self.cfg.p2p.get("max_download_gb", 4) * 1e9
        speeds, seeds, leechs, incoming, total = [], [], [], [], 0
        first_peer_s = None
        t0 = time.time()
        while time.time() < deadline:
            time.sleep(5)
            try:
                r = self._api(base, "/api/v2/torrents/info", timeout=10)
                items = r.json()
            except (requests.RequestException, ValueError):
                continue
            if not items:
                continue
            t = items[0]
            speeds.append(t.get("dlspeed", 0))
            seeds.append(t.get("num_seeds", 0))
            leechs.append(t.get("num_leechs", 0))
            total = max(total, t.get("downloaded", 0))
            if first_peer_s is None and (t.get("num_seeds", 0)
                                         + t.get("num_leechs", 0)) > 0:
                first_peer_s = round(time.time() - t0, 1)
            incoming.append(self._incoming_peers(base, t.get("hash")))
            if total > max_bytes:
                self.log("volume max atteint, arret du test P2P")
                break

        def avg(xs):
            xs = [x for x in xs if x is not None]
            return round(sum(xs) / len(xs), 2) if xs else 0

        # on ignore les 2 premiers echantillons : montee en charge du swarm
        warm = speeds[2:] or speeds
        return {
            "p2p_down_mbps": round(avg(warm) * 8 / 1e6, 2),
            "p2p_down_peak_mbps": round(max(speeds or [0]) * 8 / 1e6, 2),
            "p2p_seeds": avg(seeds),
            "p2p_leechers": avg(leechs),
            "p2p_incoming_peers": avg(incoming),
            "p2p_incoming_peak": max(incoming or [0]),
            "p2p_first_peer_s": first_peer_s,
            "p2p_downloaded_mb": round(total / 1e6, 1),
            "p2p_samples": len(speeds),
        }

    def _incoming_peers(self, base, torrent_hash):
        if not torrent_hash:
            return 0
        try:
            r = self._api(base, "/api/v2/sync/torrentPeers?hash=%s&rid=0"
                          % torrent_hash, timeout=10)
            peers = r.json().get("peers", {})
        except (requests.RequestException, ValueError, AttributeError):
            return 0
        # le drapeau "I" signale une connexion entrante (peer venu vers nous)
        return sum(1 for p in peers.values() if "I" in (p.get("flags") or ""))

    @staticmethod
    def _external_port_open(ip, port, timeout=5):
        if not ip or not port:
            return False
        try:
            with socket.create_connection((ip, int(port)), timeout=timeout):
                return True
        except OSError:
            return False
