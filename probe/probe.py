#!/usr/bin/env python3
"""Sonde de mesure reseau. Tourne dans la pile reseau testee (netns de gluetun
ou pile par defaut pour la baseline). Chaque sous-commande imprime un objet
JSON unique sur stdout ; toute erreur est capturee et rendue dans le JSON.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import statistics
import subprocess
import sys
import time

CURL = ["curl", "-sS", "--connect-timeout", "8"]
# os.devnull vaut /dev/null dans le conteneur et nul sous Windows : la sonde
# reste ainsi testable hors conteneur.
NULL_OUT = os.devnull


def sh(cmd, timeout=60):
    """Execute une commande, retourne (rc, stdout, stderr) sans jamais lever."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def out(obj):
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")


# --------------------------------------------------------------------------
# identite de sortie : IP publique, ASN, geoloc
# --------------------------------------------------------------------------
def cmd_ident(args):
    res = {"ok": False}
    sources = [
        ("https://ipinfo.io/json", ("ip", "country", "city", "org")),
        ("https://ifconfig.co/json", ("ip", "country_iso", "city", "asn_org")),
    ]
    for url, keys in sources:
        rc, so, se = sh(CURL + ["--max-time", "15", url], timeout=25)
        if rc != 0 or not so.strip():
            continue
        try:
            d = json.loads(so)
        except json.JSONDecodeError:
            continue
        res.update({
            "ok": True,
            "ip": d.get(keys[0]),
            "country": d.get(keys[1]),
            "city": d.get(keys[2]),
            "org": d.get(keys[3]),
            "source": url,
        })
        break
    m = re.match(r"AS(\d+)", str(res.get("org") or ""))
    res["asn"] = int(m.group(1)) if m else None
    return res


# --------------------------------------------------------------------------
# latence / gigue / perte
# --------------------------------------------------------------------------
def _fping(targets, count, interval_ms=200):
    """fping -C : une ligne par cible avec la liste des RTT."""
    cmd = ["fping", "-C", str(count), "-p", str(interval_ms), "-q", "-t", "1500"] + targets
    timeout = (count * interval_ms) / 1000.0 + 30
    rc, so, se = sh(cmd, timeout=timeout)
    stats = {}
    for line in (se or so).splitlines():
        if ":" not in line:
            continue
        host, _, values = line.partition(":")
        host = host.strip()
        raw = values.split()
        rtts = [float(v) for v in raw if v != "-"]
        sent = len(raw)
        if sent == 0:
            continue
        loss = 100.0 * (sent - len(rtts)) / sent
        entry = {"sent": sent, "received": len(rtts), "loss_pct": round(loss, 2)}
        if rtts:
            srt = sorted(rtts)
            jit = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
            entry.update({
                "min_ms": round(min(rtts), 3),
                "avg_ms": round(statistics.fmean(rtts), 3),
                "max_ms": round(max(rtts), 3),
                "p95_ms": round(srt[min(len(srt) - 1, int(0.95 * len(srt)))], 3),
                "jitter_ms": round(statistics.fmean(jit), 3) if jit else 0.0,
            })
        stats[host] = entry
    return stats


def cmd_latency(args):
    targets = [t for t in args.targets.split(",") if t]
    per_host = _fping(targets, args.count)
    ok = [v for v in per_host.values() if "avg_ms" in v]
    agg = {}
    if ok:
        agg = {
            "avg_ms": round(statistics.fmean(v["avg_ms"] for v in ok), 3),
            "min_ms": round(min(v["min_ms"] for v in ok), 3),
            "p95_ms": round(statistics.fmean(v["p95_ms"] for v in ok), 3),
            "jitter_ms": round(statistics.fmean(v["jitter_ms"] for v in ok), 3),
            "loss_pct": round(statistics.fmean(v["loss_pct"] for v in per_host.values()), 3),
        }
    return {"ok": bool(ok), "aggregate": agg, "per_host": per_host}


# --------------------------------------------------------------------------
# debit : endpoints Cloudflare speed, multi-flux
# --------------------------------------------------------------------------
DOWN_URL = "https://speed.cloudflare.com/__down?bytes={n}"
UP_URL = "https://speed.cloudflare.com/__up"


# UNE seule requete par flux, coupee par --max-time : enchainer les requetes
# fait tomber Cloudflare en 429 puis 403. La cible doit donc etre un gros
# fichier statique, et elle est choisie au demarrage de la campagne (pickdl)
# pour rester identique a tous les cas mesures.
def _one_download(seconds, url=None):
    url = url or DOWN_URL.format(n=1000000000)
    rc, so, se = sh(CURL + ["--max-time", str(seconds), "-o", NULL_OUT,
                            "-w", "%{size_download} %{http_code}", url],
                    timeout=seconds + 20)
    parts = so.split()
    if len(parts) >= 2:
        try:
            return float(parts[0]), parts[1]
        except ValueError:
            pass
    return 0.0, "rc%d" % rc


def _one_upload(seconds, mbytes):
    # dd fournit un flux borne, curl l'envoie en transfert chunked
    script = (
        "dd if=/dev/zero bs=1M count=%d 2>/dev/null | "
        "curl -sS --max-time %d -o /dev/null "
        "-w '%%{size_upload} %%{http_code}' -X POST "
        "-H 'Content-Type: application/octet-stream' --data-binary @- %s"
    ) % (mbytes, seconds, UP_URL)
    rc, so, se = sh(["sh", "-c", script], timeout=seconds + 25)
    parts = so.split()
    if len(parts) != 2:
        return 0.0, "curl_rc=%d %s" % (rc, se[:120])
    try:
        return float(parts[0]), parts[1]
    except ValueError:
        return 0.0, "illisible"


def _parallel(fn, streams, *fnargs):
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=streams) as ex:
        results = list(ex.map(lambda _: fn(*fnargs), range(streams)))
    wall = time.time() - t0
    total_bytes = sum(r[0] for r in results)
    info = ",".join(sorted({str(r[1]) for r in results}))
    if wall <= 0 or total_bytes <= 0:
        return 0.0, total_bytes, round(wall, 2), info
    return round(total_bytes * 8 / wall / 1e6, 2), total_bytes, round(wall, 2), info


def cmd_throughput(args):
    res = {"ok": False}
    down_mbps, down_bytes, down_wall, down_info = _parallel(
        _one_download, args.streams, args.seconds, args.url)
    res.update({"down_mbps": down_mbps, "down_bytes": down_bytes,
                "down_seconds": down_wall, "down_http": down_info,
                "down_url": args.url})
    if not args.skip_upload:
        up_streams = max(1, args.streams // 2)
        up_mbps, up_bytes, up_wall, up_info = _parallel(
            _one_upload, up_streams, args.seconds, 400)
        res.update({"up_mbps": up_mbps, "up_bytes": up_bytes,
                    "up_seconds": up_wall, "up_streams": up_streams,
                    "up_http": up_info})
    res["ok"] = res.get("down_mbps", 0) > 0
    res["streams"] = args.streams
    return res


def cmd_pickdl(args):
    """Classe les cibles de debit candidates. Appele une fois au demarrage de
    la campagne, hors tunnel, pour que tous les cas partagent la meme cible."""
    results = []
    for url in [u for u in args.urls.split(",") if u]:
        mbps, total, wall, info = _parallel(
            _one_download, args.streams, args.seconds, url)
        results.append({"url": url, "mbps": mbps,
                        "mb": round(total / 1e6, 1), "http": info})
    results.sort(key=lambda r: -r["mbps"])
    return {"ok": bool(results and results[0]["mbps"] > 0),
            "best": results[0]["url"] if results else None,
            "candidates": results}


# --------------------------------------------------------------------------
# latence sous charge (bufferbloat)
# --------------------------------------------------------------------------
def cmd_loadedlatency(args):
    targets = [t for t in args.targets.split(",") if t][:2]
    idle = _fping(targets, 10)
    idle_avg = [v["avg_ms"] for v in idle.values() if "avg_ms" in v]
    with cf.ThreadPoolExecutor(max_workers=args.streams + 1) as ex:
        dl = [ex.submit(_one_download, args.seconds, args.url)
              for _ in range(args.streams)]
        time.sleep(1)
        loaded = ex.submit(_fping, targets, max(10, args.seconds * 4), 250).result()
        for f in dl:
            f.result()
    loaded_avg = [v["avg_ms"] for v in loaded.values() if "avg_ms" in v]
    if not idle_avg or not loaded_avg:
        return {"ok": False}
    i, l = statistics.fmean(idle_avg), statistics.fmean(loaded_avg)
    return {
        "ok": True,
        "idle_ms": round(i, 2),
        "loaded_ms": round(l, 2),
        "delta_ms": round(l - i, 2),
        "ratio": round(l / i, 2) if i else None,
    }


# --------------------------------------------------------------------------
# navigation reelle : DNS / TCP / TLS / TTFB
# --------------------------------------------------------------------------
WFMT = ('{"dns":%{time_namelookup},"connect":%{time_connect},'
        '"tls":%{time_appconnect},"ttfb":%{time_starttransfer},'
        '"total":%{time_total},"code":%{http_code}}')


def cmd_web(args):
    urls = [u for u in args.urls.split(",") if u]
    per_url, all_ttfb, all_total, all_dns = {}, [], [], []
    for url in urls:
        samples = []
        for _ in range(args.repeats):
            rc, so, se = sh(CURL + ["--max-time", "25", "-o", NULL_OUT,
                                    "-L", "-w", WFMT, url], timeout=35)
            if rc != 0:
                continue
            try:
                d = json.loads(so.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                continue
            samples.append(d)
        if not samples:
            per_url[url] = {"ok": False}
            continue
        ttfb = statistics.fmean(s["ttfb"] for s in samples) * 1000
        total = statistics.fmean(s["total"] for s in samples) * 1000
        dns = statistics.fmean(s["dns"] for s in samples) * 1000
        per_url[url] = {"ok": True, "ttfb_ms": round(ttfb, 1),
                        "total_ms": round(total, 1), "dns_ms": round(dns, 1),
                        "code": samples[-1]["code"], "samples": len(samples)}
        all_ttfb.append(ttfb)
        all_total.append(total)
        all_dns.append(dns)
    agg = {}
    if all_ttfb:
        agg = {"ttfb_ms": round(statistics.fmean(all_ttfb), 1),
               "total_ms": round(statistics.fmean(all_total), 1),
               "dns_ms": round(statistics.fmean(all_dns), 1)}
    return {"ok": bool(all_ttfb), "aggregate": agg, "per_url": per_url}


# --------------------------------------------------------------------------
# fuites : IPv6, DNS, resolveurs
# --------------------------------------------------------------------------
def cmd_leaks(args):
    res = {}

    # IPv6 : depuis un tunnel v4-only, toute sortie v6 est une fuite
    rc, so, _ = sh(CURL + ["-6", "--max-time", "8",
                           "https://api64.ipify.org"], timeout=15)
    v6 = so.strip() if rc == 0 and ":" in so else None
    res["ipv6_egress_ip"] = v6
    res["ipv6_reachable"] = v6 is not None

    # Resolveurs DNS reellement utilises (IP de sortie vue par Google)
    rc, so, _ = sh(["dig", "+short", "+time=3", "+tries=1", "TXT",
                    "o-o.myaddr.l.google.com", "@ns1.google.com"], timeout=20)
    resolvers = [l.strip().strip('"') for l in so.splitlines() if l.strip()]
    res["dns_egress_ips"] = resolvers

    # Nameservers configures dans le conteneur
    try:
        with open("/etc/resolv.conf") as f:
            res["resolv_conf"] = [l.split()[1] for l in f
                                  if l.startswith("nameserver")]
    except OSError:
        res["resolv_conf"] = []

    # ASN du premier resolveur, pour comparaison avec l'ASN de sortie VPN
    res["dns_asn"] = None
    res["dns_org"] = None
    if resolvers:
        rc, so, _ = sh(CURL + ["--max-time", "10",
                               "https://ipinfo.io/%s/json" % resolvers[0]],
                       timeout=20)
        try:
            d = json.loads(so)
            res["dns_org"] = d.get("org")
            m = re.match(r"AS(\d+)", str(d.get("org") or ""))
            res["dns_asn"] = int(m.group(1)) if m else None
        except json.JSONDecodeError:
            pass

    rc, so, _ = sh(["dig", "+short", "+time=3", "cloudflare.com", "A"], timeout=15)
    res["dns_resolves"] = bool(so.strip())
    res["ok"] = True
    return res


# --------------------------------------------------------------------------
# MTU effectif du tunnel
# --------------------------------------------------------------------------
def cmd_mtu(args):
    target = args.target
    lo, hi, best = 1200, 1500, None
    # ping avec DF : payload + 28 octets d'en-tetes = MTU du chemin
    while lo <= hi:
        mid = (lo + hi) // 2
        rc, so, se = sh(["ping", "-M", "do", "-c", "1", "-W", "2",
                         "-s", str(mid - 28), target], timeout=10)
        if rc == 0:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    rc, so, _ = sh(["ip", "-o", "link", "show"], timeout=10)
    ifaces = {}
    for line in so.splitlines():
        m = re.search(r"^\d+:\s+([^:@]+).*mtu (\d+)", line)
        if m:
            ifaces[m.group(1)] = int(m.group(2))
    return {"ok": best is not None, "path_mtu": best, "interfaces": ifaces}


# --------------------------------------------------------------------------
# joignabilite binaire (sert au test de kill-switch)
# --------------------------------------------------------------------------
def cmd_reach(args):
    rc, so, _ = sh(CURL + ["--max-time", str(args.timeout), "-o", NULL_OUT,
                           "-w", "%{http_code}", args.url],
                   timeout=args.timeout + 5)
    return {"ok": True, "reachable": rc == 0, "http_code": so.strip()}


# --------------------------------------------------------------------------
# iperf3 optionnel
# --------------------------------------------------------------------------
def cmd_iperf(args):
    base = ["iperf3", "-c", args.host, "-p", str(args.port), "-J",
            "-t", str(args.seconds), "-P", str(args.streams)]
    res = {"ok": False}
    for direction, extra in (("down", ["-R"]), ("up", [])):
        rc, so, se = sh(base + extra, timeout=args.seconds + 30)
        try:
            d = json.loads(so)
            bps = d["end"]["sum_received"]["bits_per_second"]
            res["%s_mbps" % direction] = round(bps / 1e6, 2)
            res["ok"] = True
        except (json.JSONDecodeError, KeyError):
            res["%s_error" % direction] = (se or so)[:200]
    return res


# --------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(prog="probe")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ident")

    p = sub.add_parser("latency")
    p.add_argument("--targets", default="1.1.1.1,8.8.8.8,9.9.9.9")
    p.add_argument("--count", type=int, default=20)

    p = sub.add_parser("throughput")
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--streams", type=int, default=8)
    p.add_argument("--url", default=None)
    p.add_argument("--skip-upload", action="store_true")

    p = sub.add_parser("pickdl")
    p.add_argument("--urls", required=True)
    p.add_argument("--seconds", type=int, default=4)
    p.add_argument("--streams", type=int, default=8)

    p = sub.add_parser("loadedlatency")
    p.add_argument("--targets", default="1.1.1.1,8.8.8.8")
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--streams", type=int, default=8)
    p.add_argument("--url", default=None)

    p = sub.add_parser("web")
    p.add_argument("--urls", required=True)
    p.add_argument("--repeats", type=int, default=2)

    sub.add_parser("leaks")

    p = sub.add_parser("mtu")
    p.add_argument("--target", default="1.1.1.1")

    p = sub.add_parser("reach")
    p.add_argument("--url", default="https://1.1.1.1/")
    p.add_argument("--timeout", type=int, default=6)

    p = sub.add_parser("iperf")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=5201)
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--streams", type=int, default=4)
    return ap


def main():
    args = build_parser().parse_args()
    fn = globals()["cmd_" + args.cmd]
    t0 = time.time()
    try:
        result = fn(args)
    except Exception as e:  # la sonde ne doit jamais tuer le run
        result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    result["_duration_s"] = round(time.time() - t0, 2)
    result["_cmd"] = args.cmd
    out(result)


if __name__ == "__main__":
    main()
