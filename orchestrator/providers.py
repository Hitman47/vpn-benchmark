"""Selection des serveurs a tester chez chaque provider.

Une seule cle WireGuard de compte suffit pour joindre n'importe quel serveur du
provider : gluetun embarque les cles publiques et les endpoints. Ce module ne
sert donc qu'a CHOISIR les serveurs (pays, capacite P2P, charge) et a produire
les variables d'environnement gluetun qui epinglent ce serveur precis.
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

PROTON_FEATURE_P2P = 4
PROTON_FEATURE_TOR = 2


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "vpn-benchmark/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# NordVPN
# --------------------------------------------------------------------------
def _nord_country_id(country):
    data = _get_json("https://api.nordvpn.com/v1/servers/countries")
    for c in data:
        if c.get("name", "").lower() == country.lower():
            return c.get("id")
    return None


def nord_servers(country, limit, p2p_only=True):
    """Serveurs NordLynx recommandes (les moins charges) pour un pays."""
    cid = _nord_country_id(country)
    if cid is None:
        return []
    params = {
        "filters[servers_technologies][identifier]": "wireguard_udp",
        "filters[country_id]": str(cid),
        "limit": str(max(limit * 4, 20)),
    }
    if p2p_only:
        params["filters[servers_groups][identifier]"] = "legacy_p2p"
    url = ("https://api.nordvpn.com/v1/servers/recommendations?"
           + urllib.parse.urlencode(params))
    data = _get_json(url)
    servers = []
    for s in data:
        groups = [g.get("identifier") for g in s.get("groups", [])]
        servers.append({
            "provider": "nordvpn",
            "name": s.get("name"),
            "id": s.get("hostname"),
            "ip": s.get("station"),
            "country": country,
            "city": _nord_city(s),
            "load": s.get("load"),
            "p2p": "legacy_p2p" in groups,
        })
    servers.sort(key=lambda x: (x["load"] if x["load"] is not None else 100))
    return servers[:limit]


def _nord_city(s):
    for loc in s.get("locations", []):
        c = (loc.get("country") or {}).get("city") or {}
        if c.get("name"):
            return c["name"]
    return None


# --------------------------------------------------------------------------
# ProtonVPN
# --------------------------------------------------------------------------
PROTON_ENDPOINTS = (
    "https://api.protonvpn.ch/vpn/logicals",
    "https://api.protonmail.ch/vpn/logicals",
    "https://api.protonvpn.ch/vpn/v1/logicals",
)
# l'API Proton rejette les requetes sans en-tetes applicatifs (HTTP 400)
PROTON_HEADERS = {
    "User-Agent": "ProtonVPN/4.0.0 (Linux; vpn-benchmark)",
    "Accept": "application/vnd.protonmail.v1+json",
    "x-pm-appversion": "LinuxVPN_4.0.0",
    "x-pm-apiversion": "3",
}


def _proton_get():
    last = None
    for url in PROTON_ENDPOINTS:
        try:
            return _get_json(url, headers=PROTON_HEADERS)
        except Exception as e:
            last = "%s -> %s" % (url, e)
    raise RuntimeError(last or "aucun endpoint Proton joignable")


def proton_servers(country, limit, p2p_only=True):
    """Serveurs logiques Proton payants du pays, tries par charge."""
    iso = ISO.get(country, country[:2].upper())
    data = _proton_get()
    servers = []
    for s in data.get("LogicalServers", []):
        if s.get("ExitCountry") != iso:
            continue
        if s.get("Tier", 0) < 2:          # 0/1 = free/basic, 2 = Plus
            continue
        feats = int(s.get("Features", 0) or 0)
        if feats & PROTON_FEATURE_TOR:
            continue
        is_p2p = bool(feats & PROTON_FEATURE_P2P)
        if p2p_only and not is_p2p:
            continue
        if (s.get("Status", 1) or 0) != 1:
            continue
        phys = s.get("Servers") or [{}]
        servers.append({
            "provider": "protonvpn",
            "name": s.get("Name"),
            "id": s.get("Name"),
            "ip": phys[0].get("EntryIP"),
            "country": country,
            "city": s.get("City"),
            "load": s.get("Load"),
            "p2p": is_p2p,
        })
    servers.sort(key=lambda x: (x["load"] if x["load"] is not None else 100))
    return servers[:limit]


# --------------------------------------------------------------------------
def pick_servers(provider, country, limit, log=print):
    """Retourne une liste de serveurs, ou un marqueur 'pays uniquement' si
    l'API du provider est injoignable (gluetun choisira alors lui-meme)."""
    try:
        if provider == "nordvpn":
            servers = nord_servers(country, limit)
            if not servers:                       # aucun P2P dans ce pays
                servers = nord_servers(country, limit, p2p_only=False)
        elif provider == "protonvpn":
            servers = proton_servers(country, limit)
            if not servers:
                servers = proton_servers(country, limit, p2p_only=False)
        else:
            servers = []
    except Exception as e:
        log("selection serveurs %s/%s impossible (%s) -> gluetun choisira"
            % (provider, country, e))
        servers = []
    if not servers:
        return [{"provider": provider, "name": "auto-%s" % country, "id": None,
                 "ip": None, "country": country, "city": None, "load": None,
                 "p2p": None}]
    return servers


def gluetun_server_env(provider, server):
    """Variables gluetun qui epinglent le serveur choisi."""
    env = {"SERVER_COUNTRIES": server["country"]}
    if not server.get("id"):
        return env
    if provider == "nordvpn":
        env["SERVER_HOSTNAMES"] = server["id"]
    elif provider == "protonvpn":
        env["SERVER_NAMES"] = server["id"]
    return env
