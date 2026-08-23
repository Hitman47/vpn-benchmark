"""Rapport final : resume terminal, page HTML autonome, compose gluetun gagnant."""
import datetime
import html
import os

METRIC_LABELS = {
    "throughput_down_mbps": ("Debit descendant", "Mb/s"),
    "throughput_up_mbps": ("Debit montant", "Mb/s"),
    "latency_avg_ms": ("Latence moyenne", "ms"),
    "jitter_ms": ("Gigue", "ms"),
    "packet_loss_pct": ("Perte de paquets", "%"),
    "loaded_latency_delta_ms": ("Bufferbloat (latence sous charge)", "ms"),
    "web_ttfb_ms": ("TTFB web", "ms"),
    "web_total_ms": ("Chargement page complet", "ms"),
    "connect_seconds": ("Temps de connexion", "s"),
    "success_rate_pct": ("Taux de connexions reussies", "%"),
    "p2p_down_mbps": ("Debit torrent", "Mb/s"),
    "p2p_incoming_peers": ("Peers entrants", "peers"),
    "p2p_seeds": ("Seeds vus", "peers"),
    "port_forward_ok": ("Port forwarding fonctionnel", "0/1"),
    "leak_count": ("Fuites detectees", "nb"),
    "cpu_max_pct": ("CPU max du conteneur VPN", "%"),
    "path_mtu": ("MTU du chemin", "octets"),
}

PROVIDER_LABELS = {"nordvpn": "NordVPN", "protonvpn": "ProtonVPN",
                   "baseline": "Sans VPN (reference)"}
COLORS = {"nordvpn": "#3b82f6", "protonvpn": "#8b5cf6", "baseline": "#64748b"}


def label(metric):
    return METRIC_LABELS.get(metric, (metric, ""))[0]


def unit(metric):
    return METRIC_LABELS.get(metric, (metric, ""))[1]


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


# --------------------------------------------------------------------------
# resume terminal
# --------------------------------------------------------------------------
def print_summary(agg, scores, overhead, verdict, log=print):
    providers = [p for p in ("baseline", "nordvpn", "protonvpn") if p in agg]
    vpns = [p for p in providers if p != "baseline"]
    width = 34
    log("")
    log("=" * (width + 20 * len(providers)))
    header = "METRIQUE".ljust(width) + "".join(
        PROVIDER_LABELS.get(p, p)[:18].rjust(20) for p in providers)
    log(header)
    log("-" * (width + 20 * len(providers)))
    for metric in list(METRIC_LABELS):
        if not any(agg[p].get(metric) is not None for p in providers):
            continue
        line = ("%s (%s)" % (label(metric), unit(metric)))[:width].ljust(width)
        for p in providers:
            line += fmt(agg[p].get(metric)).rjust(20)
        log(line)
    log("-" * (width + 20 * len(providers)))
    line = "SCORE PONDERE /100".ljust(width)
    for p in providers:
        line += (fmt(scores[p]["score"]) if p in scores else "-").rjust(20)
    log(line)
    for p in vpns:
        o = overhead.get(p, {})
        if o:
            log("  %s vs sans VPN : debit conserve %s%% | latence +%s ms"
                % (PROVIDER_LABELS.get(p, p),
                   fmt(o.get("down_retention_pct")), fmt(o.get("latency_added_ms"))))
    log("=" * (width + 20 * len(providers)))
    if verdict.get("countries"):
        log("Pays retenus pour le score : %s" % ", ".join(verdict["countries"]))
    if verdict.get("excluded_countries"):
        log("Pays EXCLUS (un provider n'y a pas repondu) : %s"
            % ", ".join(verdict["excluded_countries"]))
    if verdict.get("winner"):
        log("VERDICT : %s (ecart %s pts, confiance %s)"
            % (PROVIDER_LABELS.get(verdict["winner"], verdict["winner"]),
               verdict.get("gap"), verdict.get("confidence")))
        for r in verdict.get("top_reasons", []):
            log("   + meilleur sur %s" % r)
    else:
        log("PAS DE VERDICT : %s" % verdict.get("reason", "donnees insuffisantes"))
    log("")


# --------------------------------------------------------------------------
# SVG minimal, sans dependance
# --------------------------------------------------------------------------
def bar_chart(title, rows, unit_txt="", width=560, higher_better=True):
    """rows = [(provider, value)]"""
    rows = [(p, v) for p, v in rows if v is not None]
    if not rows:
        return ""
    vmax = max(abs(v) for _, v in rows) or 1
    bar_h, gap, pad_l = 26, 14, 130
    height = len(rows) * (bar_h + gap) + 30
    parts = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">'
             % (width, height, html.escape(title))]
    for i, (p, v) in enumerate(rows):
        y = 20 + i * (bar_h + gap)
        w = max(2, (abs(v) / vmax) * (width - pad_l - 90))
        parts.append(
            '<text x="0" y="%d" class="lbl">%s</text>'
            % (y + 17, html.escape(PROVIDER_LABELS.get(p, p)[:16])))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" fill="%s"/>'
                     % (pad_l, y, w, bar_h, COLORS.get(p, "#94a3b8")))
        parts.append('<text x="%.1f" y="%d" class="val">%s %s</text>'
                     % (pad_l + w + 8, y + 17, fmt(v), html.escape(unit_txt)))
    parts.append("</svg>")
    return "".join(parts)


def line_chart(title, series, unit_txt="", width=760, height=240):
    """series = {provider: [(x_index, value)]}"""
    pts = [v for s in series.values() for _, v in s if v is not None]
    if len(pts) < 2:
        return ""
    vmax, vmin = max(pts), min(pts)
    span = (vmax - vmin) or 1
    xmax = max((x for s in series.values() for x, _ in s), default=1) or 1
    pad = 40
    parts = ['<svg viewBox="0 0 %d %d" class="chart">' % (width, height)]
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="axis"/>'
                 % (pad, height - pad, width - 10, height - pad))
    parts.append('<line x1="%d" y1="10" x2="%d" y2="%d" class="axis"/>'
                 % (pad, pad, height - pad))
    for p, s in series.items():
        s = [(x, v) for x, v in s if v is not None]
        if len(s) < 2:
            continue
        d = []
        for x, v in s:
            px = pad + (x / xmax) * (width - pad - 15)
            py = (height - pad) - ((v - vmin) / span) * (height - pad - 20)
            d.append("%s%.1f,%.1f" % ("M" if not d else "L", px, py))
        parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="2.5"/>'
                     % (" ".join(d), COLORS.get(p, "#94a3b8")))
    parts.append('<text x="%d" y="%d" class="ax">%s</text>'
                 % (pad, height - 12, html.escape("rounds -> " + unit_txt)))
    parts.append('<text x="2" y="18" class="ax">%s</text>' % fmt(vmax))
    parts.append('<text x="2" y="%d" class="ax">%s</text>' % (height - pad, fmt(vmin)))
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root{--bg:#ffffff;--fg:#0f172a;--muted:#64748b;--line:#e2e8f0;--card:#f8fafc;
--accent:#2563eb;--ok:#16a34a;--bad:#dc2626}
@media (prefers-color-scheme: dark){:root{--bg:#0b1120;--fg:#e2e8f0;
--muted:#94a3b8;--line:#1e293b;--card:#111a2e}}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:28px;margin:0 0 4px} h2{font-size:19px;margin:36px 0 12px}
.sub{color:var(--muted);margin-bottom:24px}
.verdict{background:var(--card);border:1px solid var(--line);border-left:5px solid
var(--accent);border-radius:10px;padding:20px 24px;margin:20px 0}
.verdict .big{font-size:26px;font-weight:650}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card .k{color:var(--muted);font-size:13px} .card .v{font-size:22px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600}
.win{color:var(--ok);font-weight:650} .lose{color:var(--muted)}
.chart{width:100%;height:auto;margin:8px 0}
.chart .lbl{font-size:13px;fill:var(--fg)} .chart .val{font-size:13px;fill:var(--muted)}
.chart .axis{stroke:var(--line);stroke-width:1} .chart .ax{font-size:11px;fill:var(--muted)}
.scroll{overflow-x:auto}
code,pre{background:var(--card);border:1px solid var(--line);border-radius:8px}
pre{padding:14px;overflow-x:auto;font-size:13px}
.note{color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
background:var(--card);border:1px solid var(--line);color:var(--muted)}
"""


def build_html(db, cfg, agg, scores, overhead, verdict, run_id):
    providers = [p for p in ("baseline", "nordvpn", "protonvpn") if p in agg]
    vpns = [p for p in providers if p != "baseline"]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    cases = db.cases(run_id)
    errors = db.events(run_id, "error")

    h = ['<title>Benchmark NordVPN vs ProtonVPN</title>',
         '<style>%s</style>' % CSS, '<div class="wrap">']
    h.append('<h1>NordVPN vs ProtonVPN</h1>')
    h.append('<div class="sub">Mode <b>%s</b> &middot; %d mesures de cas '
             '&middot; genere le %s &middot; run <code>%s</code></div>'
             % (html.escape(cfg.mode_name), len(cases), now, html.escape(run_id)))

    # --- verdict
    if not verdict.get("winner"):
        h.append('<div class="verdict"><div class="k">Pas de verdict</div>'
                 '<div class="big">Campagne inexploitable</div>'
                 '<div class="sub" style="margin:6px 0 0">%s</div></div>'
                 % html.escape(verdict.get("reason", "donnees insuffisantes")))
    if verdict.get("winner"):
        w = verdict["winner"]
        h.append('<div class="verdict"><div class="k">Gagnant</div>'
                 '<div class="big">%s &middot; %s/100</div>'
                 '<div class="sub" style="margin:6px 0 0">Ecart de %s points '
                 '&middot; confiance <b>%s</b> &middot; %d cas mesures par provider</div>'
                 % (PROVIDER_LABELS.get(w, w), fmt(scores[w]["score"]),
                    fmt(verdict.get("gap")), html.escape(verdict.get("confidence", "")),
                    min((agg[p].get("_n_cases") or 0) for p in vpns) if vpns else 0))
        if verdict.get("top_reasons"):
            h.append('<div class="note">Domine sur : %s</div>'
                     % html.escape(", ".join(verdict["top_reasons"])))
        h.append('</div>')
    if verdict.get("countries") or verdict.get("excluded_countries"):
        h.append('<div class="card"><div class="k">Perimetre de comparaison</div>'
                 '<div class="note">Pays retenus : <b>%s</b></div>'
                 % html.escape(", ".join(verdict.get("countries") or ["aucun"])))
        if verdict.get("excluded_countries"):
            h.append('<div class="note">Exclus car un seul provider y a repondu, '
                     'les comparer serait trompeur : <b>%s</b></div>'
                     % html.escape(", ".join(verdict["excluded_countries"])))
        h.append('</div>')

    # --- cartes score
    h.append('<div class="grid">')
    for p in vpns:
        o = overhead.get(p, {})
        h.append('<div class="card"><div class="k">%s</div>'
                 '<div class="v">%s/100</div>'
                 '<div class="note">debit conserve %s%% &middot; latence +%s ms</div></div>'
                 % (PROVIDER_LABELS.get(p, p), fmt(scores[p]["score"]),
                    fmt(o.get("down_retention_pct")), fmt(o.get("latency_added_ms"))))
    h.append('</div>')

    # --- tableau complet
    h.append('<h2>Toutes les metriques (medianes)</h2><div class="scroll"><table>')
    h.append('<tr><th>Metrique</th>%s<th>Poids</th></tr>'
             % "".join('<th>%s</th>' % PROVIDER_LABELS.get(p, p) for p in providers))
    for metric in METRIC_LABELS:
        vals = {p: agg[p].get(metric) for p in providers}
        if not any(v is not None for v in vals.values()):
            continue
        hib = cfg.scoring["higher_is_better"].get(metric, True)
        present = {p: v for p, v in vals.items()
                   if v is not None and p != "baseline"}
        best = None
        if present:
            best = (max if hib else min)(present, key=lambda p: present[p])
        cells = ""
        for p in providers:
            cls = "win" if (p == best and len(present) > 1) else ""
            cells += '<td class="%s">%s</td>' % (cls, fmt(vals[p]))
        h.append('<tr><td>%s <span class="badge">%s</span></td>%s<td>%s</td></tr>'
                 % (html.escape(label(metric)), html.escape(unit(metric)), cells,
                    cfg.scoring["weights"].get(metric, "-")))
    h.append('</table></div>')

    # --- graphiques cles
    h.append('<h2>Comparaison visuelle</h2>')
    for metric in ("throughput_down_mbps", "throughput_up_mbps", "latency_avg_ms",
                   "jitter_ms", "loaded_latency_delta_ms", "p2p_down_mbps",
                   "p2p_incoming_peers", "web_ttfb_ms"):
        rows = [(p, agg[p].get(metric)) for p in providers]
        svg = bar_chart(label(metric), rows, unit(metric))
        if svg:
            h.append('<div class="card"><b>%s</b> <span class="badge">%s</span>%s</div>'
                     % (html.escape(label(metric)), html.escape(unit(metric)), svg))

    # --- evolution temporelle
    series = {}
    for p in vpns:
        pts = db.metric_series(p, "throughput_down_mbps", run_id)
        series[p] = [(i, r["value"]) for i, r in enumerate(pts)]
    lc = line_chart("Debit descendant", series, "Mb/s")
    if lc:
        h.append('<h2>Stabilite dans le temps</h2>'
                 '<div class="card"><b>Debit descendant par mesure successive</b>%s'
                 '<div class="note">Une courbe plate = reseau previsible. '
                 'Les creux correspondent aux heures de pointe.</div></div>' % lc)

    # --- detail par pays
    h.append('<h2>Detail par serveur</h2><div class="scroll"><table>')
    h.append('<tr><th>Provider</th><th>Pays</th><th>Serveur</th><th>Round</th>'
             '<th>IP de sortie</th><th>Reseau</th><th>OK</th></tr>')
    for c in cases[-60:]:
        h.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                 '<td>%s</td><td>%s</td></tr>'
                 % tuple(html.escape(str(c.get(k) or "-")) for k in
                         ("provider", "country", "server", "round", "exit_ip",
                          "exit_org", "ok")))
    h.append('</table></div>')

    if errors:
        h.append('<h2>Incidents (%d)</h2><div class="scroll"><table>' % len(errors))
        h.append('<tr><th>Horodatage</th><th>Message</th></tr>')
        for e in errors[-40:]:
            ts = datetime.datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M:%S")
            h.append('<tr><td>%s</td><td>%s</td></tr>'
                     % (ts, html.escape(str(e["message"])[:300])))
        h.append('</table></div>')

    h.append('<h2>Methode</h2><div class="note"><p>Chaque cas ouvre un tunnel '
             'gluetun dedie, puis lance une sonde <b>dans la pile reseau du '
             'tunnel</b> : la mesure correspond exactement a ce que verra un '
             'conteneur applicatif en production. Les providers sont testes '
             'sequentiellement et l\'ordre est inverse a chaque round pour '
             'annuler la derive temporelle. Une mesure sans VPN (baseline) sert '
             'de reference a chaque round.</p>'
             '<p>Le score est une somme ponderee de metriques normalisees : pour '
             'chaque metrique, le meilleur provider vaut 1,0 et l\'autre une '
             'fraction proportionnelle. Les poids sont modifiables dans '
             '<code>bench.yaml</code> : recalcule le rapport avec '
             '<code>./run.sh report</code> apres modification.</p></div>')
    h.append('</div>')
    return "\n".join(h)


# --------------------------------------------------------------------------
def write_winner_compose(path, provider, agg, cfg):
    pcfg = cfg.providers[provider]
    pf = pcfg.get("port_forwarding")
    key_var = ("NORD_WIREGUARD_PRIVATE_KEY" if provider == "nordvpn"
               else "PROTON_WIREGUARD_PRIVATE_KEY")
    lines = [
        "# Genere par vpn-benchmark : configuration gluetun du provider gagnant",
        "# Renseigne %s dans un fichier .env a cote de ce compose." % key_var,
        "services:",
        "  gluetun:",
        "    image: %s" % cfg.gluetun_image,
        "    container_name: gluetun",
        "    cap_add: [NET_ADMIN]",
        "    devices: [/dev/net/tun:/dev/net/tun]",
        "    environment:",
        "      VPN_SERVICE_PROVIDER: %s" % pcfg["gluetun_provider"],
        "      VPN_TYPE: wireguard",
        "      WIREGUARD_PRIVATE_KEY: ${%s}" % key_var,
    ]
    if provider == "protonvpn":
        lines.append("      WIREGUARD_ADDRESSES: ${PROTON_WIREGUARD_ADDRESSES:-10.2.0.2/32}")
    lines += [
        "      SERVER_COUNTRIES: %s" % ",".join(cfg.m("countries", ["Netherlands"])),
        "      TZ: ${TZ:-Europe/Paris}",
        "      FIREWALL_OUTBOUND_SUBNETS: ${LAN_SUBNET:-192.168.1.0/24}",
        "      HEALTH_TARGET_ADDRESS: 1.1.1.1:443",
    ]
    if pf:
        lines += [
            "      VPN_PORT_FORWARDING: on",
            "      VPN_PORT_FORWARDING_PROVIDER: %s" % pcfg["gluetun_provider"],
        ]
    lines += [
        "    ports:",
        "      - 8000:8000    # control server",
        "      - 8080:8080    # WebUI du conteneur place derriere le tunnel",
        "    restart: unless-stopped",
        "",
        "# Exemple de conteneur derriere le tunnel :",
        "#  qbittorrent:",
        "#    image: linuxserver/qbittorrent",
        "#    network_mode: service:gluetun",
        "#    depends_on: [gluetun]",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_report(db, cfg, agg, scores, overhead, verdict, run_id, results_dir):
    os.makedirs(results_dir, exist_ok=True)
    html_path = os.path.join(results_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(db, cfg, agg, scores, overhead, verdict, run_id))
    compose = None
    if verdict.get("winner"):
        compose = write_winner_compose(
            os.path.join(results_dir, "gluetun-winner.docker-compose.yml"),
            verdict["winner"], agg, cfg)
    return html_path, compose
