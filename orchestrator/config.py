"""Configuration : bench.yaml embarque dans l'image + surcharges par variables
d'environnement (c'est ce qu'on renseigne dans l'interface Portainer)."""
import os

import yaml

RESULTS_DIR = os.environ.get("BENCH_RESULTS", "/app/results")

# un bench.yaml depose dans un volume monte sur /config a la priorite
CANDIDATES = [os.environ.get("BENCH_YAML"), "/config/bench.yaml", "/app/bench.yaml"]


def _yaml_path():
    for p in CANDIDATES:
        if p and os.path.exists(p):
            return p
    raise SystemExit("bench.yaml introuvable")


def _env_bool(name):
    v = os.environ.get(name)
    if v is None or v == "":
        return None
    return v.strip().lower() in ("1", "true", "yes", "on", "oui")


def _env_list(name):
    v = os.environ.get(name)
    if not v:
        return None
    return [x.strip() for x in v.split(",") if x.strip()]


class Config:
    def __init__(self, mode):
        path = _yaml_path()
        with open(path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f)
        self.yaml_path = path
        if mode not in self.raw["modes"]:
            raise SystemExit("mode inconnu: %s (attendu: %s)"
                             % (mode, ", ".join(self.raw["modes"])))
        self.mode_name = mode
        self.mode = self.raw["modes"][mode]
        self.providers = self.raw["providers"]
        self.targets = self.raw["targets"]
        self.p2p = self.raw["p2p"]
        self.scoring = self.raw["scoring"]
        self.runtime = self.raw.get("runtime", {})

        # --- environnement / secrets
        self.project = os.environ.get("BENCH_PROJECT", "vpnbench")
        self.self_name = os.environ.get("BENCH_SELF_NAME", "vpnbench-orchestrator")
        self.gluetun_image = os.environ.get("GLUETUN_IMAGE", "qmcgaw/gluetun:v3.40")
        self.lan_subnet = os.environ.get("LAN_SUBNET", "192.168.1.0/24")
        self.tz = os.environ.get("TZ", "UTC")
        self.gluetun_log_level = self._gluetun_log_level()
        self.http_port = int(os.environ.get("BENCH_HTTP_PORT", "8888"))
        self.keep_alive = _env_bool("BENCH_KEEP_ALIVE")
        if self.keep_alive is None:
            self.keep_alive = True

        self.keys = {
            "nordvpn": {
                "WIREGUARD_PRIVATE_KEY": os.environ.get(
                    "NORD_WIREGUARD_PRIVATE_KEY", "").strip(),
            },
            "protonvpn": {
                "WIREGUARD_PRIVATE_KEY": os.environ.get(
                    "PROTON_WIREGUARD_PRIVATE_KEY", "").strip(),
                "WIREGUARD_ADDRESSES": os.environ.get(
                    "PROTON_WIREGUARD_ADDRESSES", "10.2.0.2/32").strip(),
            },
        }
        self._apply_env_overrides()

    # ------------------------------------------------------------------
    def check_keys(self):
        missing = [n for n, kv in self.keys.items()
                   if not kv.get("WIREGUARD_PRIVATE_KEY")]
        if missing:
            raise SystemExit(
                "Cle WireGuard manquante pour : %s\n"
                "Renseigne NORD_WIREGUARD_PRIVATE_KEY et "
                "PROTON_WIREGUARD_PRIVATE_KEY dans les variables "
                "d'environnement de la stack Portainer." % ", ".join(missing))

    # ------------------------------------------------------------------
    GLUETUN_LOG_LEVELS = ("debug", "info", "warning", "error")

    def _gluetun_log_level(self):
        """Une valeur inconnue ici fait sortir gluetun en une seconde, donc
        tous les tunnels de la campagne. On la refuse au lieu de la propager."""
        v = (os.environ.get("BENCH_GLUETUN_LOG_LEVEL") or "").strip().lower()
        if not v:
            return "info"
        if v == "warn":
            return "warning"
        if v in self.GLUETUN_LOG_LEVELS:
            return v
        print("ATTENTION : BENCH_GLUETUN_LOG_LEVEL=%r n'est pas un niveau de "
              "log gluetun (attendu : %s). Valeur ignoree, 'info' utilise. "
              "Le mode de campagne se choisit avec BENCH_MODE."
              % (v, ", ".join(self.GLUETUN_LOG_LEVELS)))
        return "info"

    # ------------------------------------------------------------------
    def _apply_env_overrides(self):
        m = self.mode

        def num(env, key, cast=int, target=None):
            v = os.environ.get(env)
            if v not in (None, ""):
                (target if target is not None else m)[key] = cast(v)

        num("BENCH_ROUNDS", "rounds")
        num("BENCH_SERVERS_PER_COUNTRY", "servers_per_country")
        num("BENCH_LATENCY_COUNT", "latency_count")
        num("BENCH_THROUGHPUT_SECONDS", "throughput_seconds")
        num("BENCH_THROUGHPUT_STREAMS", "throughput_streams")
        num("BENCH_WEB_REPEATS", "web_repeats")
        num("BENCH_P2P_MINUTES", "p2p_minutes")
        num("BENCH_P2P_EVERY_ROUNDS", "p2p_every_rounds")
        num("BENCH_P2P_MAX_COUNTRIES", "p2p_max_countries")
        num("BENCH_MAX_DOWNLOAD_GB", "max_download_gb", float, self.p2p)

        v = os.environ.get("BENCH_INTERVAL_MINUTES")
        if v not in (None, ""):
            m["interval_seconds"] = int(float(v) * 60)

        for env, key in (("BENCH_COUNTRIES", "countries"),):
            lst = _env_list(env)
            if lst:
                m[key] = lst
        lst = _env_list("BENCH_PING_TARGETS")
        if lst:
            self.targets["ping"] = lst
        lst = _env_list("BENCH_WEB_URLS")
        if lst:
            self.targets["web"] = lst
        lst = _env_list("BENCH_DOWNLOAD_URLS")
        if lst:
            self.targets["download"] = lst
        lst = _env_list("BENCH_TORRENTS")
        if lst:
            self.p2p["torrents"] = lst

        b = _env_bool("BENCH_P2P_ENABLED")
        if b is not None:
            self.p2p["enabled"] = b
        b = _env_bool("BENCH_PF_AB")
        if b is not None:
            self.p2p["port_forward_ab"] = b
        b = _env_bool("BENCH_KILLSWITCH")
        if b is not None:
            m["killswitch_test"] = b
        b = _env_bool("BENCH_INCLUDE_BASELINE")
        if b is not None:
            m["include_baseline"] = b

    # ------------------------------------------------------------------
    def m(self, key, default=None):
        return self.mode.get(key, default)

    def rt(self, key, default=None):
        return self.runtime.get(key, default)

    def summary(self):
        return ("mode=%s rounds=%s intervalle=%smin pays=%s serveurs/pays=%s "
                "p2p=%s (%s min, 1 round/%s, temoin sans PF=%s)"
                % (self.mode_name, self.m("rounds"),
                   round((self.m("interval_seconds") or 0) / 60),
                   ",".join(self.m("countries", [])),
                   self.m("servers_per_country"), self.p2p.get("enabled"),
                   self.m("p2p_minutes"), self.m("p2p_every_rounds"),
                   self.p2p.get("port_forward_ab", True)))
