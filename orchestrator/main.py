"""Orchestrateur du benchmark VPN.

Boucle principale : pour chaque round, mesure la connexion nue (baseline) puis
chaque provider serveur par serveur, en inversant l'ordre a chaque round pour
que la derive temporelle du reseau ne favorise personne.
"""
import datetime
import functools
import http.server
import os
import socketserver
import sys
import threading
import time
import traceback

from . import providers as prov
from . import report as rep
from .config import Config, RESULTS_DIR
from .db import DB
from .dockerctl import Runner, VPNConfigError, VPNError
from .p2p import P2PTester
from .scoring import Scorer

DB_PATH = os.path.join(RESULTS_DIR, "results.db")


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))


class _ResultsHandler(http.server.SimpleHTTPRequestHandler):
    """Sert le dossier des resultats : le rapport se consulte au navigateur,
    sans shell sur le NAS."""

    def do_GET(self):
        if self.path in ("/", "/index.html") and                 os.path.exists(os.path.join(RESULTS_DIR, "report.html")):
            self.send_response(302)
            self.send_header("Location", "/report.html")
            self.end_headers()
            return
        return http.server.SimpleHTTPRequestHandler.do_GET(self)

    def log_message(self, fmt, *args):
        pass


def serve_results(port):
    """Petit serveur HTTP en tache de fond sur le dossier des resultats."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    handler = functools.partial(_ResultsHandler, directory=RESULTS_DIR)
    try:
        httpd = socketserver.ThreadingTCPServer(("", port), handler)
    except OSError as e:
        log("serveur de resultats indisponible sur le port %d (%s)" % (port, e))
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("rapport consultable sur http://<ip-du-nas>:%d/" % port)
    return httpd


class Bench:
    def __init__(self, cfg, db, run_id):
        self.cfg = cfg
        self.db = db
        self.run_id = run_id
        self.runner = Runner(cfg, log)
        self.p2p = P2PTester(self.runner, cfg, log)
        self.baseline_asn = None
        self.baseline_ip = None
        self.download_url = None
        self._config_errors = set()
        self._catalog = None
        self._catalog_done = False

    # ------------------------------------------------------------------
    def catalog(self):
        """Catalogue de serveurs de gluetun, recupere une seule fois."""
        if not self._catalog_done:
            self._catalog = self.runner.fetch_server_catalog()
            self._catalog_done = True
        return self._catalog

    # ------------------------------------------------------------------
    def preflight(self):
        """Tente une connexion par provider et par pays, sans rien mesurer.
        Repond a la question : mes deux cles fonctionnent-elles sur les pays
        que je veux comparer ?"""
        rows = []
        for provider in self.cfg.providers:
            for country in self.cfg.m("countries", ["Netherlands"]):
                log("%s / %s" % (provider, country))
                servers = prov.pick_servers(provider, country, 1, log,
                                            self.catalog())
                server = servers[0]
                vpn = None
                row = {"provider": provider, "country": country,
                       "server": server["name"], "ok": False, "detail": ""}
                try:
                    vpn, seconds, ipinfo = self.runner.start_vpn(
                        provider, server, pin_server=False)
                    ident = self.runner.probe(vpn, ["ident"])
                    row.update({
                        "ok": True,
                        "seconds": seconds,
                        "ip": ident.get("ip") or ipinfo.get("public_ip"),
                        "exit_country": ident.get("country") or ipinfo.get("country"),
                        "org": ident.get("org"),
                    })
                    log("  OK en %ss : %s (%s, %s)"
                        % (seconds, row["ip"], row["exit_country"], row["org"]))
                except VPNError as e:
                    row["detail"] = str(e).splitlines()[0]
                    dernier = [l for l in str(e).splitlines() if "ERROR" in l]
                    if dernier:
                        row["detail"] = dernier[-1][:300]
                    log("  ECHEC : %s" % row["detail"])
                    self._dump_failure("preflight-%s-%s" % (provider, country),
                                       str(e))
                finally:
                    if vpn is not None:
                        try:
                            vpn.remove(force=True)
                        except Exception:
                            pass
                rows.append(row)
                time.sleep(2)
        self._preflight_report(rows)
        return rows

    def _preflight_report(self, rows):
        countries = []
        for r in rows:
            if r["country"] not in countries:
                countries.append(r["country"])
        providers = list(self.cfg.providers)
        lines = ["", "=" * 78,
                 "VERIFICATION DES CLES : quels pays sont utilisables ?", "=" * 78,
                 "%-16s%s" % ("PAYS", "".join(p[:14].ljust(16) for p in providers))]
        for country in countries:
            cells = ""
            for p in providers:
                r = next((x for x in rows
                          if x["provider"] == p and x["country"] == country), None)
                cells += (("OK %s" % (r.get("exit_country") or "")) if r and r["ok"]
                          else "ECHEC").ljust(16)
            lines.append("%-16s%s" % (country[:15], cells))
        lines.append("-" * 78)

        usable = [c for c in countries
                  if all(any(x["ok"] for x in rows
                             if x["provider"] == p and x["country"] == c)
                         for p in providers)]
        if usable:
            lines.append("Pays comparables (les deux providers repondent) : %s"
                         % ", ".join(usable))
            lines.append("Mets BENCH_COUNTRIES=%s puis lance BENCH_MODE=smoke"
                         % ",".join(usable))
        else:
            lines.append("Aucun pays commun : verifie les cles et les fichiers "
                         "de /failures/")
        broken = [(r["provider"], r["country"], r["detail"])
                  for r in rows if not r["ok"]]
        for p, c, d in broken:
            lines.append("  echec %-10s %-14s %s" % (p, c, d[:120]))
        lines.append("=" * 78)
        for line in lines:
            log(line)
        try:
            path = os.path.join(RESULTS_DIR, "preflight.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            log("resultat conserve : /preflight.txt")
        except OSError:
            pass

    # ------------------------------------------------------------------
    def pick_download_target(self):
        """Choisit une fois pour toutes la cible de debit descendant : tous les
        cas doivent partager la meme, sinon la comparaison n'a aucun sens."""
        urls = self.cfg.targets.get("download") or []
        if not urls:
            return None
        if len(urls) == 1:
            return urls[0]
        log("choix de la cible de debit (%d candidates)" % len(urls))
        r = self.runner.probe(
            None, ["pickdl", "--urls", ",".join(urls), "--seconds", "4",
                   "--streams", str(self.cfg.m("throughput_streams", 8))],
            timeout=len(urls) * 40 + 60)
        for c in r.get("candidates", []):
            log("  %-52s %8s Mb/s  http=%s%s"
                % (c["url"][:52], c["mbps"], c["http"],
                   "  (limitee, ecartee)" if c.get("throttled") else ""))
        best = r.get("best") or urls[0]
        log("cible retenue : %s" % best)
        self.db.log(self.run_id, None, "info", "cible de debit : %s" % best)
        return best

    # ------------------------------------------------------------------
    def build_matrix(self):
        matrix = {}
        n = self.cfg.m("servers_per_country", 1)
        for provider in self.cfg.providers:
            servers = []
            for country in self.cfg.m("countries", ["Netherlands"]):
                picked = prov.pick_servers(provider, country, n, log,
                                           self.catalog())
                for s in picked:
                    log("  %-10s %-14s %-22s charge=%s p2p=%s"
                        % (provider, country, s["name"], s["load"], s["p2p"]))
                servers.extend(picked)
            matrix[provider] = servers
        return matrix

    # ------------------------------------------------------------------
    def metric(self, case_id, name, value, unit=None, extra=None):
        self.db.add_metric(case_id, name, value, unit, extra)

    def _dump_failure(self, case_id, text):
        """Ecrit le detail d'un echec dans results/failures/, consultable sur
        http://<ip-du-nas>:8888/failures/ sans shell sur le NAS."""
        directory = os.path.join(RESULTS_DIR, "failures")
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "%s.log" % case_id)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            log("  detail complet : /failures/%s.log" % case_id)
        except OSError as e:
            log("  impossible d'ecrire le detail de l'echec (%s)" % e)

    def _check_throttling(self, case_id, throughput):
        """Une cible qui repond 429/403 fausse la mesure : on le trace."""
        info = str(throughput.get("down_http") or "")
        if any(code in info for code in ("429", "403", "503")):
            self.db.log(self.run_id, case_id, "warn",
                        "cible de debit limitee (http %s) : mesure descendante "
                        "sous-estimee" % info)
            log("  ATTENTION : la cible de debit repond %s, valeur sous-estimee"
                % info)

    def _record_common(self, case_id, latency, throughput, loaded, web):
        if latency.get("ok"):
            a = latency["aggregate"]
            self.metric(case_id, "latency_avg_ms", a.get("avg_ms"), "ms", latency)
            self.metric(case_id, "latency_min_ms", a.get("min_ms"), "ms")
            self.metric(case_id, "latency_p95_ms", a.get("p95_ms"), "ms")
            self.metric(case_id, "jitter_ms", a.get("jitter_ms"), "ms")
            self.metric(case_id, "packet_loss_pct", a.get("loss_pct"), "%")
        if throughput.get("ok"):
            self.metric(case_id, "throughput_down_mbps",
                        throughput.get("down_mbps"), "Mb/s", throughput)
            self.metric(case_id, "throughput_up_mbps",
                        throughput.get("up_mbps"), "Mb/s")
        if loaded.get("ok"):
            self.metric(case_id, "loaded_latency_delta_ms",
                        loaded.get("delta_ms"), "ms", loaded)
            self.metric(case_id, "loaded_latency_ms", loaded.get("loaded_ms"), "ms")
        if web.get("ok"):
            a = web["aggregate"]
            self.metric(case_id, "web_ttfb_ms", a.get("ttfb_ms"), "ms", web)
            self.metric(case_id, "web_total_ms", a.get("total_ms"), "ms")
            self.metric(case_id, "web_dns_ms", a.get("dns_ms"), "ms")

    # ------------------------------------------------------------------
    def run_baseline(self, rnd):
        case_id = "%s-r%d-baseline" % (self.run_id, rnd)
        log("baseline (sans VPN)")
        ident = self.runner.probe(None, ["ident"])
        if ident.get("ok"):
            self.baseline_asn = ident.get("asn") or self.baseline_asn
            self.baseline_ip = ident.get("ip") or self.baseline_ip
        self.db.add_case({
            "case_id": case_id, "run_id": self.run_id, "round": rnd,
            "provider": "baseline", "country": ident.get("country"),
            "server": "-", "started_at": time.time(), "ok": 1,
            "exit_ip": ident.get("ip"), "exit_asn": ident.get("asn"),
            "exit_org": ident.get("org"), "exit_country": ident.get("country"),
            "note": "reference sans tunnel"})

        latency = self.runner.probe(None, ["latency", "--targets",
                                           ",".join(self.cfg.targets["ping"]),
                                           "--count", str(self.cfg.m("latency_count", 20))])
        throughput = self.runner.probe(
            None, ["throughput", "--seconds", str(self.cfg.m("throughput_seconds", 10)),
                   "--streams", str(self.cfg.m("throughput_streams", 8))]
            + (["--url", self.download_url] if self.download_url else []))
        loaded = self.runner.probe(
            None, ["loadedlatency", "--targets", ",".join(self.cfg.targets["ping"][:2]),
                   "--seconds", str(self.cfg.m("throughput_seconds", 10)),
                   "--streams", str(self.cfg.m("throughput_streams", 8))]
            + (["--url", self.download_url] if self.download_url else []))
        web = self.runner.probe(None, ["web", "--urls", ",".join(self.cfg.targets["web"]),
                                       "--repeats", str(self.cfg.m("web_repeats", 2))])
        self._record_common(case_id, latency, throughput, loaded, web)
        self._check_throttling(case_id, throughput)
        log("  baseline : %s Mb/s down, %s Mb/s up, %s ms  [http %s | %s]"
            % (throughput.get("down_mbps"), throughput.get("up_mbps"),
               (latency.get("aggregate") or {}).get("avg_ms"),
               throughput.get("down_http"), throughput.get("up_http")))

    # ------------------------------------------------------------------
    def run_case(self, provider, server, rnd, do_p2p):
        case_id = "%s-r%d-%s-%s" % (self.run_id, rnd, provider,
                                    (server["name"] or "auto").replace("#", "_"))
        log("%s / %s / %s" % (provider, server["country"], server["name"]))
        case = {"case_id": case_id, "run_id": self.run_id, "round": rnd,
                "provider": provider, "variant": "base",
                "country": server["country"],
                "server": server["name"], "started_at": time.time(), "ok": 0,
                "exit_ip": None, "exit_asn": None, "exit_org": None,
                "exit_country": None, "note": None}

        vpn = connect_s = ipinfo = None
        failures = []
        # 1er essai sur le serveur choisi, 2e essai en laissant gluetun piocher
        # dans le pays : sa liste embarquee ignore parfois un serveur recent.
        for attempt, pin in enumerate((True, False)):
            try:
                vpn, connect_s, ipinfo = self.runner.start_vpn(
                    provider, server, pin_server=pin)
                if attempt:
                    log("  repli reussi : serveur choisi par gluetun dans %s"
                        % server["country"])
                    case["note"] = "repli : serveur non epingle"
                break
            except VPNError as e:
                fatal = isinstance(e, VPNConfigError)
                failures.append("=== essai %d (%s) ===\n%s"
                                % (attempt + 1,
                                   "serveur epingle" if pin else "pays seul", e))
                log("  ECHEC connexion (essai %d, %s) :"
                    % (attempt + 1, "epingle" if pin else "pays seul"))
                for line in str(e).splitlines():
                    log("    | %s" % line)
                if fatal:
                    # une valeur d'environnement invalide ne se rattrape pas en
                    # changeant de serveur : inutile de gaspiller un essai
                    self._config_errors.add(provider)
                    break
                if pin and server.get("id"):
                    log("  nouvel essai sans epingler le serveur")

        if vpn is None:
            case["note"] = "\n".join(failures)[:2000]
            self.db.add_case(case)
            self.db.log(self.run_id, case_id, "error",
                        "connexion impossible %s/%s:\n%s"
                        % (provider, server["name"], "\n".join(failures)))
            self._dump_failure(case_id, "\n\n".join(failures))
            self.metric(case_id, "connect_failed", 1)
            return

        try:
            self.metric(case_id, "connect_seconds", connect_s, "s")
            log("  tunnel etabli en %ss (IP %s)" % (connect_s, ipinfo.get("public_ip")))

            ident = self.runner.probe(vpn, ["ident"])
            case.update({"ok": 1, "exit_ip": ident.get("ip") or ipinfo.get("public_ip"),
                         "exit_asn": ident.get("asn"), "exit_org": ident.get("org"),
                         "exit_country": ident.get("country") or ipinfo.get("country")})
            self.db.add_case(case)
            log("  sortie : %s (%s) %s" % (case["exit_ip"], case["exit_country"],
                                           case["exit_org"]))

            # --- securite / fuites
            leaks = self.runner.probe(vpn, ["leaks"])
            leak_count, details = self._score_leaks(leaks, case)
            self.metric(case_id, "leak_count", leak_count, "nb", details)

            mtu = self.runner.probe(vpn, ["mtu"])
            if mtu.get("ok"):
                self.metric(case_id, "path_mtu", mtu.get("path_mtu"), "octets", mtu)

            # --- performances
            latency = self.runner.probe(
                vpn, ["latency", "--targets", ",".join(self.cfg.targets["ping"]),
                      "--count", str(self.cfg.m("latency_count", 20))])
            sampler = self.runner.cpu_sampler(vpn)
            try:
                sampler.start()
            except Exception as e:
                log("  echantillonnage CPU indisponible (%s)" % e)
            throughput = self.runner.probe(
                vpn, ["throughput", "--seconds", str(self.cfg.m("throughput_seconds", 10)),
                      "--streams", str(self.cfg.m("throughput_streams", 8))]
                + (["--url", self.download_url] if self.download_url else []))
            try:
                cpu = sampler.stop()
            except Exception as e:   # jamais au detriment des mesures reseau
                log("  echantillonnage CPU ignore (%s)" % e)
                cpu = {}
            self.metric(case_id, "cpu_avg_pct", cpu.get("cpu_avg_pct"), "%")
            self.metric(case_id, "cpu_max_pct", cpu.get("cpu_max_pct"), "%")
            loaded = self.runner.probe(
                vpn, ["loadedlatency", "--targets", ",".join(self.cfg.targets["ping"][:2]),
                      "--seconds", str(self.cfg.m("throughput_seconds", 10)),
                      "--streams", str(self.cfg.m("throughput_streams", 8))]
                + (["--url", self.download_url] if self.download_url else []))
            web = self.runner.probe(
                vpn, ["web", "--urls", ",".join(self.cfg.targets["web"]),
                      "--repeats", str(self.cfg.m("web_repeats", 2))])
            self._record_common(case_id, latency, throughput, loaded, web)
            self._check_throttling(case_id, throughput)
            log("  %s Mb/s down, %s Mb/s up, %s ms, CPU max %s%%  [http %s]"
                % (throughput.get("down_mbps"), throughput.get("up_mbps"),
                   (latency.get("aggregate") or {}).get("avg_ms"),
                   cpu.get("cpu_max_pct"), throughput.get("down_http")))
            if (cpu.get("cpu_max_pct") or 0) > 90:
                self.db.log(self.run_id, case_id, "warn",
                            "CPU du conteneur VPN sature : le serveur, pas le VPN, "
                            "peut etre le facteur limitant")

            # --- iperf3 optionnel
            for t in self.cfg.targets.get("iperf3") or []:
                r = self.runner.probe(vpn, ["iperf", "--host", t["host"], "--port",
                                            str(t.get("port", 5201)), "--seconds", "10"])
                if r.get("ok"):
                    self.metric(case_id, "iperf_down_mbps", r.get("down_mbps"), "Mb/s", r)
                    self.metric(case_id, "iperf_up_mbps", r.get("up_mbps"), "Mb/s")

            # --- P2P
            if do_p2p and self.cfg.p2p.get("enabled"):
                log("  test P2P (%s min)" % self.cfg.m("p2p_minutes", 5))
                p = self.p2p.run(provider, vpn, case["exit_ip"],
                                 self.cfg.m("p2p_minutes", 5))
                self._record_p2p(case_id, p)

            # --- kill-switch : le trafic doit mourir avec le tunnel
            if self.cfg.m("killswitch_test") and rnd == 0:
                self._killswitch(case_id, vpn)

        except Exception as e:
            self.db.log(self.run_id, case_id, "error",
                        "%s: %s" % (type(e).__name__, e))
            log("  erreur : %s" % e)
            traceback.print_exc()
        finally:
            if vpn is not None:
                try:
                    vpn.remove(force=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def _score_leaks(self, leaks, case):
        """Compte les fuites reelles. Reference : l'ASN de la connexion nue."""
        count, details = 0, {}
        if not leaks.get("ok"):
            return None, {"error": leaks.get("error")}
        if leaks.get("ipv6_reachable"):
            count += 1
            details["ipv6"] = leaks.get("ipv6_egress_ip")
        dns_asn = leaks.get("dns_asn")
        if dns_asn and self.baseline_asn and dns_asn == self.baseline_asn:
            count += 1
            details["dns"] = "resolveur sur l'ASN du FAI (%s)" % leaks.get("dns_org")
        if case.get("exit_ip") and self.baseline_ip and \
                case["exit_ip"] == self.baseline_ip:
            count += 5
            details["tunnel"] = "IP de sortie identique a la connexion nue"
        if not leaks.get("dns_resolves"):
            details["dns_broken"] = True
        details["resolv_conf"] = leaks.get("resolv_conf")
        details["dns_egress"] = leaks.get("dns_egress_ips")
        return count, details

    def _killswitch(self, case_id, vpn):
        log("  test kill-switch")
        if not self.runner.set_vpn_status(vpn, "stopped"):
            self.db.log(self.run_id, case_id, "warn",
                        "kill-switch non testable (control server)")
            return
        time.sleep(3)
        r = self.runner.probe(vpn, ["reach", "--url", "https://1.1.1.1/",
                                    "--timeout", "6"], timeout=30)
        leaked = 1 if r.get("reachable") else 0
        self.metric(case_id, "killswitch_leak", leaked, "0/1", r)
        if leaked:
            self.db.log(self.run_id, case_id, "error",
                        "FUITE : trafic sortant alors que le tunnel est coupe")
            log("    FUITE detectee")
        else:
            log("    etanche")
        self.runner.set_vpn_status(vpn, "running")

    # ------------------------------------------------------------------
    P2P_METRICS = ("p2p_down_mbps", "p2p_up_mbps", "p2p_down_peak_mbps",
                   "p2p_seeds", "p2p_leechers", "p2p_incoming_peers",
                   "p2p_incoming_peak", "p2p_connected_peers", "p2p_peers_peak",
                   "p2p_first_peer_s", "p2p_downloaded_mb")

    def _record_p2p(self, case_id, p):
        for k in self.P2P_METRICS:
            self.metric(case_id, k, p.get(k))
        self.metric(case_id, "port_forward_ok", p.get("port_forward_ok", 0),
                    "0/1", p)
        log("    torrent : %s Mb/s down, %s Mb/s up, %s peers dont %s entrants, "
            "port %s"
            % (p.get("p2p_down_mbps"), p.get("p2p_up_mbps"),
               p.get("p2p_connected_peers"), p.get("p2p_incoming_peers"),
               "redirige %s" % p.get("forwarded_port")
               if p.get("port_forward_ok") else "non joignable"))
        if p.get("error"):
            log("    test P2P incomplet : %s" % p["error"])

    # ------------------------------------------------------------------
    def run_pf_ab(self, provider, server, rnd):
        """Bras temoin : meme provider, meme serveur, meme torrent, mais NAT-PMP
        coupe. Le delta avec le cas normal mesure ce que le port forwarding
        apporte reellement, sans comparer deux providers differents.

        Enchaine immediatement le cas normal pour que le swarm soit dans un
        etat aussi proche que possible."""
        case_id = "%s-r%d-%s-nopf-%s" % (self.run_id, rnd, provider,
                                         (server["name"] or "auto").replace("#", "_"))
        log("%s / %s / %s / SANS port forwarding (temoin)"
            % (provider, server["country"], server["name"]))
        case = {"case_id": case_id, "run_id": self.run_id, "round": rnd,
                "provider": provider, "variant": "nopf",
                "country": server["country"], "server": server["name"],
                "started_at": time.time(), "ok": 0, "exit_ip": None,
                "exit_asn": None, "exit_org": None, "exit_country": None,
                "note": "temoin A/B : VPN_PORT_FORWARDING=off"}
        vpn = None
        try:
            try:
                vpn, connect_s, ipinfo = self.runner.start_vpn(
                    provider, server, pin_server=bool(server.get("id")),
                    port_forwarding=False)
            except VPNError:
                # meme repli que pour un cas normal : gluetun choisit dans le pays
                vpn, connect_s, ipinfo = self.runner.start_vpn(
                    provider, server, pin_server=False, port_forwarding=False)
                case["note"] += " (serveur non epingle)"
            ident = self.runner.probe(vpn, ["ident"])
            case.update({"ok": 1,
                         "exit_ip": ident.get("ip") or ipinfo.get("public_ip"),
                         "exit_asn": ident.get("asn"), "exit_org": ident.get("org"),
                         "exit_country": ident.get("country")})
            self.db.add_case(case)
            self.metric(case_id, "connect_seconds", connect_s, "s")
            p = self.p2p.run(provider, vpn, case["exit_ip"],
                             self.cfg.m("p2p_minutes", 5), port_forwarding=False)
            self._record_p2p(case_id, p)
        except Exception as e:
            case["note"] = "temoin A/B echoue : %s" % e
            self.db.add_case(case)
            self.db.log(self.run_id, case_id, "error", "temoin sans PF : %s" % e)
            log("  temoin sans port forwarding indisponible : %s"
                % str(e).splitlines()[0])
        finally:
            if vpn is not None:
                try:
                    vpn.remove(force=True)
                except Exception:
                    pass

    def p2p_countries(self):
        """Pays ou le test torrent est joue. Il coute cher (et jusqu'a deux fois
        p2p_minutes chez Proton avec le bras temoin) : le limiter aux premiers
        pays de la liste evite qu'un round deborde son intervalle, sans rien
        perdre puisque le swarm est le meme partout."""
        countries = list(self.cfg.m("countries", []))
        limit = self.cfg.m("p2p_max_countries", 0) or 0
        keep = countries[:limit] if limit > 0 else countries
        if keep != countries:
            log("test torrent limite a : %s (sur %s)"
                % (", ".join(keep), ", ".join(countries)))
        return keep

    def _pf_ab_providers(self):
        """Providers pour lesquels le test A/B du port forwarding a un sens."""
        if not self.cfg.p2p.get("port_forward_ab", True):
            return []
        return [p for p, c in self.cfg.providers.items() if c.get("port_forwarding")]

    # ------------------------------------------------------------------
    def run(self):
        rounds = self.cfg.m("rounds", 1)
        interval = self.cfg.m("interval_seconds", 0)
        p2p_every = self.cfg.m("p2p_every_rounds", 1)
        p2p_countries = self.p2p_countries()
        self.download_url = self.pick_download_target()
        log("selection des serveurs")
        matrix = self.build_matrix()
        order = list(self.cfg.providers)

        for rnd in range(rounds):
            t_round = time.time()
            log("=== round %d/%d ===" % (rnd + 1, rounds))
            if self.cfg.m("include_baseline", True):
                try:
                    self.run_baseline(rnd)
                except Exception as e:
                    self.db.log(self.run_id, None, "error", "baseline: %s" % e)
            seq = order if rnd % 2 == 0 else list(reversed(order))
            do_p2p = (p2p_every > 0) and (rnd % p2p_every == 0)
            ab = self._pf_ab_providers() if do_p2p and self.cfg.p2p.get("enabled") else []
            for provider in seq:
                done_p2p = set()
                for server in matrix[provider]:
                    country = server["country"]
                    # meme liste de pays P2P pour les deux providers, sinon la
                    # comparaison torrent porterait sur des pays differents
                    case_p2p = (do_p2p and country in p2p_countries
                                and country not in done_p2p)
                    self.run_case(provider, server, rnd, case_p2p)
                    if len(self._config_errors) >= 2:
                        log("ARRET : gluetun refuse la configuration pour les "
                            "deux providers. Corrige la variable signalee "
                            "ci-dessus dans la stack Portainer, puis relance.")
                        self.db.log(self.run_id, None, "error",
                                    "campagne interrompue : configuration "
                                    "gluetun invalide pour les deux providers")
                        return
                    time.sleep(self.cfg.rt("between_cases_seconds", 5))
                    if case_p2p:
                        done_p2p.add(country)
                        if provider in ab:
                            # meme serveur, port forwarding coupe : on isole la
                            # redirection de port, pas le serveur ni le pays
                            self.run_pf_ab(provider, server, rnd)
                            time.sleep(self.cfg.rt("between_cases_seconds", 5))
            if rnd < rounds - 1 and interval > 0:
                wait = max(0, interval - (time.time() - t_round))
                if wait:
                    log("attente %d min avant le round suivant" % round(wait / 60))
                    time.sleep(wait)


# --------------------------------------------------------------------------
def finalize(cfg, db, run_id):
    scorer = Scorer(db, cfg, run_id)
    agg = scorer.aggregate()
    scores = scorer.score(agg)
    overhead = scorer.overhead_vs_baseline(agg)
    verdict = scorer.verdict(scores, agg)
    pf_ab = scorer.port_forward_ab()
    rep.print_summary(agg, scores, overhead, verdict, log, pf_ab)
    html_path, compose = rep.write_report(db, cfg, agg, scores, overhead,
                                          verdict, run_id, RESULTS_DIR, pf_ab)
    csvs = db.export_csv(RESULTS_DIR)
    log("rapport   : %s" % html_path)
    if compose:
        log("compose   : %s" % compose)
    for c in csvs:
        log("csv       : %s" % c)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BENCH_MODE", "smoke")
    mode = (mode or "smoke").strip().lower()

    if mode == "report":
        db = DB(DB_PATH)
        run_id = db.last_run_id()
        if not run_id:
            raise SystemExit("aucun run en base : lance d'abord le mode smoke")
        row = db.conn.execute("SELECT mode FROM runs WHERE run_id=?",
                              (run_id,)).fetchone()
        cfg = Config(row["mode"] if row else "smoke")
        serve_results(cfg.http_port)
        log("regeneration du rapport pour le run %s" % run_id)
        finalize(cfg, db, run_id)
        idle(cfg)
        return

    cfg = Config(mode)
    cfg.check_keys()
    serve_results(cfg.http_port)
    log(cfg.summary())

    if mode == "preflight":
        db = DB(DB_PATH)
        run_id = "preflight-%s" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        db.start_run(run_id, mode, cfg.raw)
        bench = Bench(cfg, db, run_id)
        bench.runner.cleanup()
        try:
            bench.preflight()
        finally:
            bench.runner.cleanup()
            db.finish_run(run_id)
        idle(cfg)
        return

    db = DB(DB_PATH)
    run_id = "%s-%s" % (mode, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    db.start_run(run_id, mode, cfg.raw)
    log("run %s" % run_id)

    bench = Bench(cfg, db, run_id)
    bench.runner.cleanup()
    try:
        bench.run()
    except KeyboardInterrupt:
        log("interruption : on passe au rapport avec ce qui est deja mesure")
    finally:
        bench.runner.cleanup()
        db.finish_run(run_id)
        finalize(cfg, db, run_id)
    idle(cfg)


def idle(cfg):
    """Le conteneur reste en vie pour servir le rapport. Pour relancer une
    campagne : redeployer ou redemarrer la stack depuis Portainer."""
    if not cfg.keep_alive:
        return
    log("campagne terminee. Le conteneur reste actif pour servir le rapport "
        "sur le port %d ; redemarre la stack pour relancer une campagne."
        % cfg.http_port)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
