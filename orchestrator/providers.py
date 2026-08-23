"""Selection des serveurs a tester chez chaque provider.

Une seule cle WireGuard de compte suffit pour joindre n'importe quel serveur du
provider : gluetun substitue l'endpoint et la cle publique du serveur choisi.
Ce module ne sert donc qu'a CHOISIR les serveurs et a produire les variables
d'environnement qui les epinglent.

Source de verite : le catalogue embarque dans gluetun (servers.json). C'est le
seul qui garantisse que gluetun acceptera le serveur, et l'API ProtonVPN exige
desormais un token d'authentification. L'API NordVPN, elle, reste ouverte : on
s'en sert uniquement pour connaitre la charge et tester les serveurs les moins
occupes.
"""
import json
import urllib.parse
import urllib.request

TIMEOUT = 20

ISO = {
    "Netherlands": "NL", "Switzerland": "CH", "Sweden": "SE", "France": "FR",
    "Germany": "DE", "Belgium": "BE", "United States": "US", "Canada": "CA",
    "Spain": "ES", "Italy": "IT", "United Kingdom": "GB", "Luxembourg": "LU",
    "Iceland": "IS", "Romania": "RO", "Poland": "PL", "Austria": "AT",
    "Norway": "NO", "Finland": "FI", "Denmark": "DK", "Ireland": "IE",
    "Portugal": "PT", "Czech Republic": "CZ", "Czechia": "CZ",
}


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "vpn-benchmark/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _first(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# --------------------------------------------------------------------------
# catalogue gluetun
# --------------------------------------------------------------------------
def catalog_servers(catalog, provider, country, limit, p2p_only=True):
    """Serveurs du provider dans ce pays, tels que gluetun les connait."""
    if not catalog:
        return []
    block = catalog.get(provider) or {}
    entries = block.get("servers") or []
    wanted = country.strip().lower()
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if str(_first(e, "country") or "").strip().lower() != wanted:
            continue
        if str(_first(e, "vpn") or "wireguard").lower() not in ("wireguard", ""):
            continue
        if not _first(e, "wgpubkey", "wg_pub_key"):
            continue          # pas de cle publique = pas utilisable en WireGuard
        if e.get("free") or e.get("secure_core") or e.get("tor"):
            continue
        name = _first(e, "server_name", "name", "hostname")
        # nom lisible dans le rapport ; l'epinglage se fait sur le hostname
        label = name
        if provider == "nordvpn" and e.get("number"):
            label = "%s #%s" % (country, e["number"])
        out.append({
            "provider": provider,
            "name": label,
            "id": name,
            "hostname": _first(e, "hostname"),
            "ip": (e.get("ips") or [None])[0],
            "country": country,
            "city": _first(e, "city"),
            "load": None,
            "p2p": bool(e.get("portforward")) if provider == "protonvpn" else None,
        })
    if p2p_only and provider == "protonvpn":
        p2p = [s for s in out if s["p2p"]]
        if p2p:
            out = p2p
    out.sort(key=lambda s: str(s["name"]))
    return out[:limit] if limit else out


# --------------------------------------------------------------------------
# NordVPN : API ouverte, sert a connaitre la charge
# --------------------------------------------------------------------------
def _nord_country_id(country):
    for c in _get_json("https://api.nordvpn.com/v1/servers/countries"):
        if c.get("name", "").lower() == country.lower():
            return c.get("id")
    return None


def nord_load_by_hostname(country, p2p_only=True):
    """{hostname: charge} pour un pays. Best effort : {} si l'API ne repond pas."""
    try:
        cid = _nord_country_id(country)
        if cid is None:
            return {}
        params = {
            "filters[servers_technologies][identifier]": "wireguard_udp",
            "filters[country_id]": str(cid),
            "limit": "200",
        }
        if p2p_only:
            params["filters[servers_groups][identifier]"] = "legacy_p2p"
        data = _get_json("https://api.nordvpn.com/v1/servers/recommendations?"
                         + urllib.parse.urlencode(params))
        return {s.get("hostname"): s.get("load") for s in data if s.get("hostname")}
    except Exception:
        return {}


# --------------------------------------------------------------------------
def pick_servers(provider, country, limit, log=print, catalog=None):
    """Retourne une liste de serveurs a tester, ou un marqueur 'pays seul' si
    aucune selection n'est possible (gluetun choisira alors lui-meme)."""
    servers = []
    try:
        servers = catalog_servers(catalog, provider, country, None)
    except Exception as e:
        log("lecture du catalogue impossible pour %s/%s (%s)"
            % (provider, country, e))

    if servers and provider == "nordvpn":
        # on garde l'ordre du catalogue mais on privilegie les serveurs les
        # moins charges d'apres l'API Nord, quand elle repond
        loads = nord_load_by_hostname(country)
        if loads:
            for s in servers:
                s["load"] = loads.get(s["hostname"])
            known = [s for s in servers if s["load"] is not None]
            if known:
                servers = sorted(known, key=lambda s: s["load"])

    if not servers:
        return [{"provider": provider, "name": "auto-%s" % country, "id": None,
                 "hostname": None, "ip": None, "country": country, "city": None,
                 "load": None, "p2p": None}]
    return servers[:limit]


def gluetun_server_env(provider, server):
    """Variables gluetun qui epinglent le serveur choisi."""
    env = {"SERVER_COUNTRIES": server["country"]}
    if not server.get("id"):
        return env
    if provider == "nordvpn":
        env["SERVER_HOSTNAMES"] = server.get("hostname") or server["id"]
    elif provider == "protonvpn":
        env["SERVER_NAMES"] = server["id"]
    return env
