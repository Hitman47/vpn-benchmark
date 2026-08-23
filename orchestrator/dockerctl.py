"""Pilotage des conteneurs : gluetun (le tunnel) et la sonde.

L'orchestrateur s'auto-inspecte au demarrage pour decouvrir sa propre image,
son reseau et ses volumes. Il n'a donc besoin d'aucun chemin de l'hote : la
stack se deploie telle quelle depuis Portainer.

La sonde partage la pile reseau de gluetun (network_mode=container:...), donc
elle mesure exactement ce que verra ton conteneur applicatif en production.
"""
import io
import json
import os
import re
import socket
import tarfile
import threading
import time

import docker
import requests

from .providers import gluetun_server_env

CONTROL_PORT = 8000
PROBE_ENTRY = ["python", "-u", "/app/probe.py"]

# gluetun valide CHAQUE route declaree et refuse de demarrer si l'une d'elles
# lui est inconnue. La liste varie d'une version a l'autre, donc on part d'un
# jeu minimal et on retire automatiquement celles qu'il rejette (voir
# _unsupported_route).
DEFAULT_AUTH_ROUTES = [
    "GET /v1/publicip/ip",
    "GET /v1/openvpn/status",
    "PUT /v1/openvpn/status",
    "GET /v1/openvpn/portforwarded",
    "GET /v1/vpn/status",
    "PUT /v1/vpn/status",
]

AUTH_TOML = """# genere par vpn-benchmark : acces local sans authentification
[[roles]]
name = "bench"
routes = [
%s
]
auth = "none"
"""

ROUTE_REJECTED = re.compile(
    r"route not supported by the control server:\s*([A-Z]+ /\S+)")


def _unsupported_route(message):
    m = ROUTE_REJECTED.search(message or "")
    return m.group(1).strip() if m else None


class VPNError(RuntimeError):
    pass


class Runner:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:
            raise SystemExit(
                "Socket Docker inaccessible (%s).\n"
                "La stack doit monter /var/run/docker.sock dans le conteneur." % e)
        self.me = self._self_container()
        self.image = self._self_image()
        self.network = self._self_network()
        self.net_subnet = self._network_subnet()
        self.volumes = self._self_volumes()
        self.auth_routes = list(DEFAULT_AUTH_ROUTES)
        self.auth_volume = self._write_auth_config()
        log("image de sonde : %s" % self.image)
        log("reseau         : %s (%s)" % (self.network, self.net_subnet))
        log("volumes        : %s" % (self.volumes or "aucun"))

    # ------------------------------------------------------------------
    # auto-decouverte
    # ------------------------------------------------------------------
    def _self_container(self):
        for name in (self.cfg.self_name, socket.gethostname()):
            if not name:
                continue
            try:
                return self.client.containers.get(name)
            except docker.errors.APIError:
                pass
        # dernier recours : identifiant lu dans le cgroup / mountinfo
        for path in ("/proc/self/mountinfo", "/proc/self/cgroup"):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    m = re.search(r"([0-9a-f]{64})", f.read())
                if m:
                    return self.client.containers.get(m.group(1))
            except (OSError, docker.errors.APIError):
                continue
        raise SystemExit(
            "Impossible de m'identifier parmi les conteneurs Docker.\n"
            "Verifie que le service porte bien container_name: %s"
            % self.cfg.self_name)

    def _self_image(self):
        img = (self.me.attrs.get("Config") or {}).get("Image")
        if img and not img.startswith("sha256:"):
            return img
        return self.me.attrs.get("Image") or img

    def _self_network(self):
        nets = self.me.attrs["NetworkSettings"]["Networks"]
        for name in nets:
            if name != "host":
                return name
        # pas de reseau exploitable : on en cree un
        name = "%s-net" % self.cfg.project
        try:
            self.client.networks.get(name)
        except docker.errors.NotFound:
            self.client.networks.create(name, driver="bridge")
        return name

    def _network_subnet(self):
        try:
            net = self.client.networks.get(self.network)
            return net.attrs["IPAM"]["Config"][0]["Subnet"]
        except (docker.errors.APIError, KeyError, IndexError, TypeError):
            return "172.16.0.0/12"

    def _self_volumes(self):
        vols = {}
        for m in self.me.attrs.get("Mounts") or []:
            if m.get("Type") == "volume" and m.get("Name"):
                vols[m.get("Destination")] = m["Name"]
        return vols

    def _write_auth_config(self):
        """Ecrit la config d'authentification de gluetun dans un volume partage.
        Retourne le nom du volume, ou None si rien n'a pu etre ecrit."""
        vol = self.volumes.get("/shared/auth")
        if not vol or not self.auth_routes:
            return None
        body = ",\n".join('  "%s"' % r for r in self.auth_routes)
        try:
            os.makedirs("/shared/auth", exist_ok=True)
            with open("/shared/auth/config.toml", "w", encoding="utf-8") as f:
                f.write(AUTH_TOML % body)
            return vol
        except OSError as e:
            self.log("config auth gluetun non ecrite (%s)" % e)
            return None

    # ------------------------------------------------------------------
    def fetch_server_catalog(self):
        """Recupere servers.json depuis gluetun lui-meme.

        C'est la seule liste qui fasse autorite : gluetun refuse tout serveur
        qui n'y figure pas, et l'API ProtonVPN exige desormais un token. On
        demarre gluetun sans cle : il ecrit son catalogue puis s'arrete sur
        l'erreur de configuration, et on extrait le fichier du conteneur.
        """
        name = "%s-catalog" % self.cfg.project
        try:
            self.client.containers.get(name).remove(force=True)
        except docker.errors.APIError:
            pass
        container = None
        try:
            container = self.client.containers.run(
                self.cfg.gluetun_image, name=name, detach=True,
                environment={"VPN_SERVICE_PROVIDER": "nordvpn",
                             "VPN_TYPE": "wireguard", "LOG_LEVEL": "info"},
                network=self.network, labels={"vpnbench": "1"})
            container.wait(timeout=90)   # sortie en erreur attendue : pas de cle
            bits, _ = container.get_archive("/gluetun/servers.json")
            buf = io.BytesIO(b"".join(bits))
            with tarfile.open(fileobj=buf) as tar:
                member = next(m for m in tar.getmembers() if m.isfile())
                data = json.loads(tar.extractfile(member).read().decode("utf-8"))
            counts = {k: len(v.get("servers") or [])
                      for k, v in data.items() if isinstance(v, dict)}
            self.log("catalogue gluetun : %s"
                     % ", ".join("%s=%d" % (k, n) for k, n in sorted(counts.items())
                                 if n))
            return data
        except Exception as e:
            self.log("catalogue gluetun indisponible (%s) : selection par pays "
                     "uniquement" % e)
            return None
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.APIError:
                    pass

    # ------------------------------------------------------------------
    def cleanup(self, prefix=None):
        prefix = prefix or self.cfg.project + "-"
        for c in self.client.containers.list(all=True):
            if c.id == self.me.id or not c.name.startswith(prefix):
                continue
            if c.name == self.cfg.self_name:
                continue
            try:
                c.remove(force=True)
            except docker.errors.APIError:
                pass

    # ------------------------------------------------------------------
    # gluetun
    # ------------------------------------------------------------------
    def start_vpn(self, provider, server, extra_env=None, pin_server=True):
        """Demarre gluetun. pin_server=False ignore le serveur choisi et laisse
        gluetun piocher dans le pays : utile en repli quand la liste embarquee
        de gluetun ne connait pas encore ce serveur.

        Si gluetun rejette une route de la config d'authentification (la liste
        change selon les versions), on la retire et on reessaie."""
        for _ in range(len(DEFAULT_AUTH_ROUTES) + 1):
            try:
                return self._start_vpn_once(provider, server, extra_env, pin_server)
            except VPNError as e:
                bad = _unsupported_route(str(e))
                if not bad or bad not in self.auth_routes:
                    raise
                self.auth_routes.remove(bad)
                self.auth_volume = self._write_auth_config()
                self.log("route '%s' inconnue de cette version de gluetun : "
                         "retiree de la config d'auth, nouvel essai" % bad)
        raise VPNError("config d'authentification gluetun irreconciliable")

    def _start_vpn_once(self, provider, server, extra_env=None, pin_server=True):
        name = "%s-vpn" % self.cfg.project
        try:
            self.client.containers.get(name).remove(force=True)
        except docker.errors.APIError:
            pass

        pcfg = self.cfg.providers[provider]
        env = {
            "VPN_SERVICE_PROVIDER": pcfg["gluetun_provider"],
            "VPN_TYPE": "wireguard",
            "TZ": self.cfg.tz,
            "DOT": "off",   # DNS-over-TLS off : on veut mesurer le resolveur du VPN
            "FIREWALL_OUTBOUND_SUBNETS": "%s,%s" % (self.net_subnet,
                                                    self.cfg.lan_subnet),
            "FIREWALL_INPUT_PORTS": "8000,8080",
            "HTTP_CONTROL_SERVER_ADDRESS": ":%d" % CONTROL_PORT,
            "HEALTH_TARGET_ADDRESS": "1.1.1.1:443",
            "LOG_LEVEL": os.environ.get("BENCH_GLUETUN_LOG_LEVEL", "info"),
            "UPDATER_PERIOD": "0",
        }
        env.update(self.cfg.keys[provider])
        if pin_server:
            env.update(gluetun_server_env(provider, server))
        else:
            env["SERVER_COUNTRIES"] = server["country"]
        if pcfg.get("port_forwarding"):
            env["VPN_PORT_FORWARDING"] = "on"
            env["VPN_PORT_FORWARDING_PROVIDER"] = pcfg["gluetun_provider"]
        env.update(extra_env or {})

        volumes = {}
        if self.auth_volume:
            volumes[self.auth_volume] = {"bind": "/gluetun/auth", "mode": "ro"}

        t0 = time.time()
        container = self.client.containers.run(
            self.cfg.gluetun_image, name=name, detach=True,
            cap_add=["NET_ADMIN"], devices=["/dev/net/tun:/dev/net/tun:rwm"],
            sysctls={"net.ipv4.conf.all.src_valid_mark": "1"},
            environment=env, network=self.network, volumes=volumes,
            labels={"vpnbench": "1"},
        )
        ip = self._wait_ready(container, t0)
        return container, round(time.time() - t0, 2), ip

    def _container_ip(self, container):
        container.reload()
        nets = container.attrs["NetworkSettings"]["Networks"]
        if self.network in nets and nets[self.network].get("IPAddress"):
            return nets[self.network]["IPAddress"]
        for n in nets.values():
            if n.get("IPAddress"):
                return n["IPAddress"]
        raise VPNError("pas d'IP pour %s" % container.name)

    def control_url(self, container, path):
        return "http://%s:%d%s" % (self._container_ip(container), CONTROL_PORT, path)

    def diagnose(self, container, header):
        """Rassemble tout ce qui explique un echec : code de sortie, message
        d'erreur du moteur, et la totalite des logs du conteneur."""
        parts = [header]
        try:
            container.reload()
            state = container.attrs.get("State") or {}
            parts.append("etat=%s code_sortie=%s oom=%s"
                         % (state.get("Status"), state.get("ExitCode"),
                            state.get("OOMKilled")))
            if state.get("Error"):
                parts.append("erreur moteur : %s" % state["Error"])
        except docker.errors.APIError as e:
            parts.append("inspection impossible : %s" % e)
        try:
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")
        except docker.errors.APIError as e:
            logs = "(logs illisibles : %s)" % e
        logs = logs.strip()
        parts.append("--- logs gluetun ---")
        parts.append(logs if logs else "(le conteneur n'a rien ecrit)")
        return "\n".join(parts)

    def _wait_ready(self, container, t0):
        """Attend que gluetun annonce une IP publique via son control server."""
        timeout = self.cfg.rt("gluetun_ready_timeout", 120)
        last_err = None
        while time.time() - t0 < timeout:
            container.reload()
            if container.status not in ("running", "created"):
                raise VPNError(self.diagnose(
                    container, "gluetun s'est arrete avant d'etablir le tunnel"))
            try:
                r = requests.get(self.control_url(container, "/v1/publicip/ip"),
                                 timeout=4)
                if r.status_code == 200:
                    d = r.json()
                    if d.get("public_ip"):
                        return d
                elif r.status_code == 401:
                    raise VPNError(
                        "control server protege : le volume /shared/auth n'est "
                        "pas monte ou la config d'auth n'a pas ete ecrite")
            except (requests.RequestException, ValueError) as e:
                last_err = e
            time.sleep(2)
        raise VPNError(self.diagnose(
            container, "tunnel non etabli en %ss (dernier essai : %s)"
                       % (timeout, last_err)))

    def forwarded_port(self, container):
        for path in ("/v1/openvpn/portforwarded", "/v1/portforwarded"):
            try:
                r = requests.get(self.control_url(container, path), timeout=5)
                if r.status_code == 200:
                    d = r.json()
                    port = d.get("port") or (d.get("ports") or [None])[0]
                    if port:
                        return int(port)
            except (requests.RequestException, ValueError, VPNError, TypeError):
                continue
        return None

    def set_vpn_status(self, container, status):
        """status = 'stopped' | 'running' (sert au test de kill-switch)."""
        body = json.dumps({"status": status})
        for path in ("/v1/vpn/status", "/v1/openvpn/status"):
            try:
                r = requests.put(self.control_url(container, path), data=body,
                                 timeout=10)
                if r.status_code in (200, 202):
                    return True
            except (requests.RequestException, VPNError):
                continue
        return False

    # ------------------------------------------------------------------
    # sonde
    # ------------------------------------------------------------------
    def probe(self, vpn_container, args, timeout=None):
        """Lance la sonde dans la pile reseau donnee et renvoie le JSON."""
        timeout = timeout or self.cfg.rt("probe_timeout", 240)
        kwargs = {
            "image": self.image,
            "command": PROBE_ENTRY + [str(a) for a in args],
            "detach": True,
            "name": "%s-probe-%d" % (self.cfg.project,
                                     int(time.time() * 1000) % 1000000),
            "environment": {"TZ": self.cfg.tz},
            "labels": {"vpnbench": "1"},
        }
        if vpn_container is not None:
            kwargs["network_mode"] = "container:%s" % vpn_container.name
        else:
            kwargs["network"] = self.network
        try:
            c = self.client.containers.run(**kwargs)
        except docker.errors.APIError as e:
            return {"ok": False, "error": "creation sonde: %s" % e}
        try:
            res = c.wait(timeout=timeout)
            raw = c.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            err = c.logs(stdout=False, stderr=True).decode("utf-8", "replace")
            for line in reversed(raw.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            return {"ok": False, "error": "sortie sonde illisible",
                    "exit_code": res.get("StatusCode"), "stderr": err[-500:]}
        except Exception as e:
            return {"ok": False, "error": "probe: %s" % e}
        finally:
            try:
                c.remove(force=True)
            except docker.errors.APIError:
                pass

    # ------------------------------------------------------------------
    # mesure CPU du conteneur VPN (detecte un NAS qui plafonne avant le VPN)
    # ------------------------------------------------------------------
    def cpu_sampler(self, container):
        return _CPUSampler(container)


class _CPUSampler(threading.Thread):
    def __init__(self, container, interval=2.0):
        super().__init__(daemon=True)
        self.container = container
        self.interval = interval
        self.samples = []
        # NE PAS nommer cet attribut _stop : Thread._stop() existe deja
        # et join() l'appelle en interne.
        self._halt = threading.Event()

    def run(self):
        prev = None
        while not self._halt.is_set():
            try:
                s = self.container.stats(stream=False)
                pct = _cpu_percent(s, prev)
                if pct is not None:
                    self.samples.append(pct)
                prev = s
            except Exception:
                pass
            self._halt.wait(self.interval)

    def stop(self):
        """Ne doit jamais faire echouer la mesure en cours : au pire on rend
        des valeurs vides."""
        try:
            self._halt.set()
            self.join(timeout=5)
        except Exception:
            pass
        if not self.samples:
            return {"cpu_avg_pct": None, "cpu_max_pct": None}
        return {"cpu_avg_pct": round(sum(self.samples) / len(self.samples), 1),
                "cpu_max_pct": round(max(self.samples), 1)}


def _cpu_percent(s, prev=None):
    try:
        cpu = s["cpu_stats"]
        pre = s["precpu_stats"] if prev is None else prev["cpu_stats"]
        d_cpu = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        d_sys = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        ncpu = cpu.get("online_cpus") or len(
            cpu["cpu_usage"].get("percpu_usage") or [1])
        if d_sys > 0 and d_cpu >= 0:
            return 100.0 * d_cpu / d_sys * ncpu
    except (KeyError, TypeError, ZeroDivisionError):
        return None
    return None
