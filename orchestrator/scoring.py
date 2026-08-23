"""Agregation des mesures et calcul du score pondere."""
import statistics


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def p95(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 3)


class Scorer:
    def __init__(self, db, cfg, run_id=None):
        self.db = db
        self.cfg = cfg
        self.run_id = run_id
        self.weights = cfg.scoring["weights"]
        self.hib = cfg.scoring["higher_is_better"]

    # ------------------------------------------------------------------
    def comparable_countries(self):
        """Pays ou CHAQUE provider VPN a au moins un cas reussi. Comparer un
        provider mesure aux Pays-Bas a un autre mesure en France n'aurait
        aucun sens : ces pays-la sont exclus du score."""
        cases = self.db.cases(self.run_id)
        vpns = sorted({c["provider"] for c in cases if c["provider"] != "baseline"})
        if not vpns:
            return [], []
        per_provider = [
            {c["country"] for c in cases if c["provider"] == p and c["ok"]}
            for p in vpns
        ]
        common = set.intersection(*per_provider) if per_provider else set()
        seen = []
        for c in cases:
            if c["provider"] != "baseline" and c["country"] not in seen:
                seen.append(c["country"])
        return ([c for c in seen if c in common],
                [c for c in seen if c not in common])

    def aggregate(self):
        """Mediane de chaque metrique par provider (baseline incluse)."""
        agg = {}
        keep, excluded = self.comparable_countries()
        self.kept_countries, self.excluded_countries = keep, excluded
        for provider in self.db.providers_seen(self.run_id):
            countries = None if provider == "baseline" else keep
            cases = [c for c in self.db.cases(self.run_id)
                     if c["provider"] == provider
                     and (countries is None or c["country"] in countries)]
            row = {}
            for metric in self.weights:
                if metric == "success_rate_pct":
                    row[metric] = (round(100.0 * sum(1 for c in cases if c["ok"])
                                         / len(cases), 1) if cases else None)
                    continue
                row[metric] = median(
                    self.db.metric_values(provider, metric, self.run_id, countries))
            # metriques de contexte, hors score
            for metric in ("throughput_down_mbps", "latency_avg_ms",
                           "p2p_down_mbps", "cpu_max_pct", "path_mtu",
                           "web_total_ms", "throughput_up_mbps"):
                row.setdefault(metric, median(
                    self.db.metric_values(provider, metric, self.run_id, countries)))
            row["_p95_latency_ms"] = p95(
                self.db.metric_values(provider, "latency_avg_ms", self.run_id,
                                      countries))
            row["_n_cases"] = len(cases)
            row["_n_ok"] = sum(1 for c in cases if c["ok"])
            agg[provider] = row
        return agg

    # ------------------------------------------------------------------
    def score(self, agg):
        """Score 0-100 par provider VPN (la baseline sert de reference, pas
        de concurrent). Normalisation relative : le meilleur prend 1.0, les
        autres une fraction proportionnelle."""
        vpns = [p for p in agg if p != "baseline"]
        out = {}
        for p in vpns:
            out[p] = {"points": 0.0, "max_points": 0.0, "details": {}}

        for metric, weight in self.weights.items():
            vals = {p: agg[p].get(metric) for p in vpns}
            present = {p: v for p, v in vals.items() if v is not None}
            if not present:
                continue
            higher = self.hib.get(metric, True)
            if higher:
                best = max(present.values())
                # si le meilleur vaut 0, personne n'a rien obtenu : 0 point
                norm = {p: (v / best if best > 0 else 0.0)
                        for p, v in present.items()}
            else:
                best = min(present.values())
                if best <= 0:
                    # 0 est le meilleur possible (ex: 0 fuite) : binaire
                    norm = {p: (1.0 if v <= 0 else 0.0) for p, v in present.items()}
                else:
                    norm = {p: min(1.0, best / v) if v > 0 else 1.0
                            for p, v in present.items()}
            for p in vpns:
                d = {"value": vals.get(p), "weight": weight}
                if p in norm:
                    d["norm"] = round(norm[p], 3)
                    d["points"] = round(norm[p] * weight, 2)
                    out[p]["points"] += norm[p] * weight
                    out[p]["max_points"] += weight
                else:
                    d["norm"] = None
                    d["points"] = 0.0
                out[p]["details"][metric] = d

        for p in vpns:
            mp = out[p]["max_points"] or 1
            out[p]["score"] = round(100.0 * out[p]["points"] / mp, 1)
            out[p]["points"] = round(out[p]["points"], 2)
        return out

    # ------------------------------------------------------------------
    def overhead_vs_baseline(self, agg):
        """Cout du VPN par rapport a la connexion nue."""
        base = agg.get("baseline")
        if not base:
            return {}
        res = {}
        for p, row in agg.items():
            if p == "baseline":
                continue
            r = {}
            b_down = base.get("throughput_down_mbps")
            if b_down and row.get("throughput_down_mbps"):
                r["down_retention_pct"] = round(
                    100.0 * row["throughput_down_mbps"] / b_down, 1)
            b_up = base.get("throughput_up_mbps")
            if b_up and row.get("throughput_up_mbps"):
                r["up_retention_pct"] = round(
                    100.0 * row["throughput_up_mbps"] / b_up, 1)
            b_lat = base.get("latency_avg_ms")
            if b_lat and row.get("latency_avg_ms"):
                r["latency_added_ms"] = round(row["latency_avg_ms"] - b_lat, 2)
            b_ttfb = base.get("web_ttfb_ms")
            if b_ttfb and row.get("web_ttfb_ms"):
                r["ttfb_added_ms"] = round(row["web_ttfb_ms"] - b_ttfb, 1)
            res[p] = r
        return res

    # ------------------------------------------------------------------
    def verdict(self, scores, agg):
        vpns = sorted(scores, key=lambda p: scores[p]["score"], reverse=True)
        if not vpns:
            return {"winner": None, "reason": "aucune donnee"}
        if not any((agg[p].get("_n_ok") or 0) > 0 for p in vpns):
            return {"winner": None, "ranking": vpns, "confidence": "nulle",
                    "countries": getattr(self, "kept_countries", []),
                    "excluded_countries": getattr(self, "excluded_countries", []),
                    "reason": "aucun tunnel n'a pu etre etabli : rien a comparer. "
                              "Voir les fichiers de /failures/ et la section "
                              "Incidents du rapport."}
        winner = vpns[0]
        gap = (scores[winner]["score"] - scores[vpns[-1]]["score"]
               if len(vpns) > 1 else 0)
        reasons = []
        for metric, d in sorted(scores[winner]["details"].items(),
                                key=lambda kv: -kv[1]["points"]):
            if d.get("norm") != 1.0 or d.get("value") is None:
                continue
            # on ne cite que les metriques ou l'ecart est reel, pas une egalite
            others = [scores[o]["details"].get(metric, {}).get("norm")
                      for o in vpns if o != winner]
            if any(n is not None and n >= 0.98 for n in others):
                continue
            reasons.append("%s = %s" % (metric, d["value"]))
            if len(reasons) == 4:
                break
        confidence = "faible"
        n = min((agg[p].get("_n_cases") or 0) for p in vpns) if vpns else 0
        if gap >= 8 and n >= 20:
            confidence = "elevee"
        elif gap >= 4 and n >= 6:
            confidence = "moyenne"
        return {"winner": winner, "gap": round(gap, 1),
                "confidence": confidence, "top_reasons": reasons,
                "ranking": vpns,
                "countries": getattr(self, "kept_countries", []),
                "excluded_countries": getattr(self, "excluded_countries", [])}
