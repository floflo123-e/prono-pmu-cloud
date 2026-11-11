"""Prono PMU - version cloud (GitHub Actions), fonctionne PC eteint.

Toutes les 10 min (9h-23h, heure de Paris) :
  - resultats des paris conseilles deja courus ;
  - si une course avec un cheval tres solide part dans 25 a 55 min, Claude donne le pari final
    (la notification sert de rappel "30 min avant") ;
  - Quinte+ : avis de Claude 25 a 55 min avant le depart ;
  - chat : repond aux messages envoyes depuis l'appli.
L'etat est relu depuis le canal ntfy (aucun fichier a sauvegarder).

Variables d'environnement : NTFY_TOPIC (secret), CLAUDE_CODE_OAUTH_TOKEN (secret).
Usage : python prono_cloud.py [tick|chat|candidates]
"""
from __future__ import annotations
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Paris")
PMU = "https://online.turfinfo.api.pmu.fr/rest/client/1/programme"
NTFY = "https://ntfy.sh"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STAKE = 2.0                 # 2 EUR par cheval
TAKE_PLACE = 0.18
MIN_RAPPORT = 1.10
HORSES_PER_RACE = 4         # 4 chevaux en simple place par course
RACES_PER_DAY = 3           # 3 courses par jour
AFTERNOON = (10, 20)        # departs entre 10h et 20h (heure de Paris)
PLAN_FROM_HOUR = 8          # choix des 3 courses des 8h (avant les courses de 10h)
WINDOW = (25, 44)           # conseil final 25-44 min avant le depart (1 seul rappel cote appli)
SYSTEM = "Tu es Claude, le conseiller hippique personnel de l'utilisateur dans son appli Prono PMU. Réponds uniquement en JSON."
SYSTEM_CHAT = ("Tu es Claude, le conseiller hippique personnel de l'utilisateur dans son appli Prono PMU. "
               "Réponds en français simple, tutoiement, phrases courtes, sans markdown lourd. Sois honnête : "
               "aucun pari n'est sûr, rappelle-le quand c'est utile, sans faire la morale à chaque message.")
WEB_CHAT = (" Tu as WebSearch et WebFetch : sers-t'en vraiment, comme un turfiste sérieux. Dès que la question porte "
            "sur une course, un cheval ou un pari, fais 3 à 6 recherches (pronostics de la presse : Paris-Turf, Equidia, "
            "Geny, Zone-Turf, Turfomania, Tierce Magazine ; non-partants ; forme récente, musique, jockey/driver, "
            "entraîneur, terrain, déferrage) et ouvre avec WebFetch 1 à 3 pages utiles pour lire les vrais pronos. "
            "Croise avec les données PMU fournies (cotes, probabilités), qui priment en cas de conflit. Dans ta réponse, "
            "dis brièvement ce que disent les pronostiqueurs (consensus ou non) et cite tes sources par leur nom. "
            "Pour une simple discussion sans course, ne cherche pas. Reste concis : le message est lu sur un téléphone.")

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 prono-pmu-cloud/1.0 usage-personnel"


def now():
    return dt.datetime.now(TZ)


def get(url):
    for i in range(3):
        try:
            time.sleep(0.2)
            r = S.get(url, timeout=30)
            if r.status_code in (204, 404):
                return None
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2 * (i + 1))
    return None


def fmt(d):
    return d.strftime("%d%m%Y")


def start_of(course):
    return dt.datetime.fromtimestamp(course["heureDepart"] / 1000, TZ)


def nb_places(n):
    return 3 if n >= 8 else (2 if n >= 4 else 0)


# ------------------------------------------------------------------ ntfy (etat + publication)
def read_channel(topic=None):
    r = S.get(f"{NTFY}/{topic or TOPIC}/json", params={"poll": "1", "since": "all"}, timeout=30)
    out = []
    for line in r.text.splitlines():
        try:
            m = json.loads(line)
            if m.get("event") == "message":
                m["body"] = json.loads(m.get("message") or "{}")
                out.append(m)
        except ValueError:
            continue
    return sorted(out, key=lambda m: m["time"])


def state():
    """Reconstitue l'etat du jour a partir des messages du canal."""
    today = now().date().isoformat()
    bets, quinte, chat, model, plan = {}, None, [], None, []
    boxes = set()
    for m in read_channel():
        b = m["body"]
        k = b.get("kind")
        if k == "bet":
            bets[b["bet"]["id"]] = b["bet"]
        elif k == "result" and b.get("bet_id") in bets:
            bets[b["bet_id"]].update(statut=b["statut"])
        elif k == "cancel" and b.get("bet_id") in bets:
            bets[b["bet_id"]]["statut"] = "annule"
        elif k == "quinte" and b["quinte"].get("date") == today:
            quinte = b["quinte"]
        elif k == "model" and b.get("date") == today:
            model = b
        elif k == "plan" and b.get("date") == today:
            plan = b.get("courses", [])
        elif k in ("chat_q", "chat_a"):
            chat.append({**b, "time": m["time"]})
        elif k == "chat_ping" and str(b.get("box", "")).isalnum() and 8 <= len(str(b.get("box", ""))) <= 32:
            boxes.add(b["box"])
    todays = {k: v for k, v in bets.items() if v.get("date") == today}
    return todays, quinte, chat, model, boxes, plan


def publish(kind, title, payload, priority=3, topic=None):
    body = json.dumps({"kind": kind, **payload}, ensure_ascii=False, separators=(",", ":"))
    # ntfy transforme en piece jointe au-dela de 4 Ko : on raccourcit le texte au lieu d'echouer
    while len(body.encode()) > 3900 and len(payload.get("text") or "") > 200:
        over = len(body.encode()) - 3900
        payload = {**payload, "text": payload["text"][:-max(over // 2, 40)].rstrip() + "…"}
        body = json.dumps({"kind": kind, **payload}, ensure_ascii=False, separators=(",", ":"))
    if len(body.encode()) > 4000:
        raise ValueError("message trop long")
    title = " ".join(title.split())[:200]
    r = S.post(f"{NTFY}/{topic or TOPIC}", data=body.encode(),
               headers={"Title": title.encode(), "Priority": str(priority), "Tags": "horse"}, timeout=30)
    r.raise_for_status()


# ------------------------------------------------------------------ Claude
def claude_bin():
    if shutil.which("claude"):
        return "claude"
    subprocess.run(["npm", "install", "-g", "--silent", "@anthropic-ai/claude-code"], check=True)
    return "claude"


def ask(prompt, system=SYSTEM, as_json=True, tools="", turns=6):
    extra = ["--allowedTools", tools, "--max-turns", str(turns)] if tools else []
    out = subprocess.run([claude_bin(), "-p", "--output-format", "json", "--tools", tools, *extra, "--system-prompt", system],
                         input=prompt, capture_output=True, text=True, timeout=420)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-400:] or out.stdout[-400:])
    text = json.loads(out.stdout).get("result", "")
    if not as_json:
        return text.strip()
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


# ------------------------------------------------------------------ calculs de marche
def market_probs(ps):
    q = {p["numPmu"]: (1 / p["dernierRapportDirect"]["rapport"]) ** 0.85 for p in ps}
    s = sum(q.values())
    return {k: v / s for k, v in q.items()}


def place_distribution(q, k):
    out, nums = [], list(q)

    def rec(chosen, prob, rest):
        if len(chosen) == k:
            out.append((tuple(chosen), prob))
            return
        for n in nums:
            if n not in chosen:
                rec(chosen + [n], prob * q[n] / rest, rest - q[n])

    rec([], 1.0, 1.0)
    return out


def place_pool(d, r, c):
    j = get(f"{PMU}/{fmt(d)}/R{r}/C{c}/combinaisons?specialisation=INTERNET")
    for comb in (j or {}).get("combinaisons", []):
        if comb["pariType"] == "E_SIMPLE_PLACE":
            return {x["combinaison"][0]: x["totalEnjeu"] for x in comb["listeCombinaisons"]}
    return {}


def evaluate(d, r, c, ps):
    k = nb_places(len(ps))
    if not k:
        return k, {}
    q = market_probs(ps)
    dist = place_distribution(q, k)
    pool = place_pool(d, r, c)
    total = sum(pool.values())
    cotes = {p["numPmu"]: p["dernierRapportDirect"]["rapport"] for p in ps}
    res = {}
    for n in q:
        p_place = sum(p for s, p in dist if n in s)
        rapports = []
        if pool.get(n):
            net = total * (1 - TAKE_PLACE)
            rapports = [(max(MIN_RAPPORT, 1 + (net - sum(pool.get(x, 0) for x in s)) / (k * pool[n])), p)
                        for s, p in dist if n in s]
        if rapports:
            w = sum(p for _, p in rapports)
            mean = sum(r_ * p for r_, p in rapports) / w
            lo = min(r_ for r_, _ in rapports)
            sig = [r_ for r_, p in rapports if p / w > 0.02]
            hi = max(sig) if sig else mean
        else:
            mean = max(MIN_RAPPORT, 1 + (cotes[n] - 1) * 0.3)
            lo, hi = MIN_RAPPORT, mean * 1.3
        res[n] = {"p_place": round(p_place, 3), "rapport_min": round(lo, 2),
                  "rapport_moyen": round(mean, 2), "rapport_max": round(hi, 2)}
    return k, res


def partants(d, r, c, all_=False):
    j = get(f"{PMU}/{fmt(d)}/R{r}/C{c}/participants") or {}
    ps = j.get("participants", [])
    return ps if all_ else [p for p in ps if p.get("statut") == "PARTANT"
                            and (p.get("dernierRapportDirect") or {}).get("rapport", 0) > 0]


def races(d, lo_min, hi_min, all_countries=False):
    prog = (get(f"{PMU}/{fmt(d)}") or {}).get("programme", {})
    t = now()
    for reunion in prog.get("reunions", []):
        if not all_countries and reunion.get("pays", {}).get("code") != "FRA":
            continue
        for course in reunion.get("courses", []):
            st = start_of(course)
            if t + dt.timedelta(minutes=lo_min) <= st <= t + dt.timedelta(minutes=hi_min):
                yield reunion, course


def race_info(d, reunion, course, ps, top=4):
    r, c = reunion["numOfficiel"], course["numOrdre"]
    k, ev = evaluate(d, r, c, ps)
    if not k:
        return None
    best = sorted(ps, key=lambda p: p["dernierRapportDirect"]["rapport"])[:top]
    return {
        "course": f"R{r}C{c}", "r": r, "c": c, "nom": course.get("libelle"),
        "hippodrome": reunion["hippodrome"]["libelleCourt"], "depart": start_of(course).strftime("%H:%M"),
        "depart_ts": course["heureDepart"], "discipline": course.get("discipline"),
        "distance_m": course.get("distance"), "partants": len(ps), "chevaux_payes_en_place": k,
        "terrain": (course.get("penetrometre") or {}).get("intitule"),
        "chevaux": [{"num": p["numPmu"], "nom": p["nom"], "cote": p["dernierRapportDirect"]["rapport"],
                     "tendance_cote_pct": p["dernierRapportDirect"].get("nombreIndicateurTendance"),
                     "musique": p.get("musique"), "jockey_driver": p.get("driver"),
                     "entraineur": p.get("entraineur"), **ev.get(p["numPmu"], {})} for p in best],
    }


def has_place(course):
    return any(p.get("typePari") in ("E_SIMPLE_PLACE", "SIMPLE_PLACE") for p in course.get("paris", []))


def candidates(d, lo_min, hi_min, max_races=8, top=4):
    found = []
    for reunion, course in races(d, lo_min, hi_min):
        if not has_place(course):
            continue
        ps = partants(d, reunion["numOfficiel"], course["numOrdre"])
        if len(ps) < 4:
            continue
        found.append((min(p["dernierRapportDirect"]["rapport"] for p in ps), reunion, course, ps))
    found.sort(key=lambda x: x[0])
    out = []
    for _, reunion, course, ps in found[:max_races]:
        try:
            info = race_info(d, reunion, course, ps, top)
        except Exception:
            info = None
        if info:
            out.append(info)
    return out


def score4(race):
    """Nombre moyen de chevaux payes parmi les 4 plus surs (max 3)."""
    ps = sorted((h.get("p_place", 0) for h in race["chevaux"]), reverse=True)[:HORSES_PER_RACE]
    return round(sum(ps), 3)


# ------------------------------------------------------------------ etapes
PROMPT_BET = """L'utilisateur joue {n} chevaux en SIMPLE PLACÉ dans cette course, {stake:.0f} € chacun ({total:.0f} € au total).
Il recevra ton conseil maintenant, environ 30 minutes avant le départ : c'est le pari final, il va le jouer.
Objectif : le maximum de chevaux payés (3 places payées, donc au mieux 3 chevaux sur 4 gagnants).

Données PMU en direct ({heure}), les {top} favoris :
- p_place : probabilité estimée d'être payé en placé (prudente),
- rapport_min / moyen / max : rapport placé estimé pour 1 € d'après les enjeux déjà joués.
Musique : chiffre = place (0 = au-delà de 9), lettre = discipline (a attelé, m monté, p plat, h haies, s steeple), D = disqualifié, T = tombé, A = arrêté, (25) = année.

{data}

Choisis exactement {n} chevaux DIFFÉRENTS parmi ceux-ci : ceux qui ont le plus de chances d'être payés.
Méfie-toi du trot (D récents), des obstacles (T), des musiques irrégulières, des cotes qui montent fort.
Réponds UNIQUEMENT :
{{"chevaux": [{{"num": <numéro>, "probabilite": <0-1>, "pourquoi": "<1 à 2 phrases, tutoiement>"}}, ...{n} fois],
 "pourquoi": "<2 à 3 phrases sur la course>", "risques": "<1 à 2 phrases>"}}"""


def minutes_until(hour):
    t = now()
    return (t.replace(hour=hour, minute=0, second=0, microsecond=0) - t).total_seconds() / 60


def step_plan(d, plan, bets):
    """Des 8h : choisit les 3 courses de la journee (10h-20h) ou 4 favoris ont le plus de chances d'etre payes."""
    if len(plan) >= RACES_PER_DAY or not (PLAN_FROM_HOUR <= now().hour < AFTERNOON[1]):
        return f"plan : {', '.join(p['course'] + ' ' + p['depart'] for p in plan) or '-'}"
    lo, hi = max(WINDOW[0], minutes_until(AFTERNOON[0])), minutes_until(AFTERNOON[1])
    if hi <= lo:
        return "plus de course aujourd'hui"
    taken = {p["course"] for p in plan} | {b["course"] for b in bets.values()}
    races_ = [c for c in candidates(d, lo, hi, max_races=60, top=HORSES_PER_RACE)
              if c["chevaux_payes_en_place"] == 3 and c["course"] not in taken]
    races_.sort(key=lambda c: -score4(c))
    add = [{"course": c["course"], "r": c["r"], "c": c["c"], "hippodrome": c["hippodrome"],
            "depart": c["depart"], "depart_ts": c["depart_ts"], "score": score4(c)}
           for c in races_[:RACES_PER_DAY - len(plan)]]
    if not add:
        return "aucune course de 8 partants ou plus aujourd'hui"
    plan[:] = sorted(plan + add, key=lambda p: p["depart_ts"])
    publish("plan", "Courses du jour choisies", {"date": d.isoformat(), "courses": plan, "silent": True}, priority=1)
    return f"plan publie : {', '.join(p['course'] + ' ' + p['depart'] for p in plan)}"


def find_race(d, r, c):
    prog = (get(f"{PMU}/{fmt(d)}") or {}).get("programme", {})
    for reunion in prog.get("reunions", []):
        if reunion["numOfficiel"] == r:
            for course in reunion.get("courses", []):
                if course["numOrdre"] == c:
                    return reunion, course
    return None, None


def step_bet(d, bets, plan):
    t = now()
    done = {b["course"] for b in bets.values() if b["statut"] != "annule"}
    due = [p for p in plan if p["course"] not in done
           and WINDOW[0] * 60 <= p["depart_ts"] / 1000 - t.timestamp() <= WINDOW[1] * 60]
    if not due:
        return "aucune course prevue dans la fenetre"
    log = []
    for p in due:
        reunion, course = find_race(d, p["r"], p["c"])
        ps = partants(d, p["r"], p["c"]) if course else []
        race = race_info(d, reunion, course, ps, top=HORSES_PER_RACE + 2) if len(ps) >= HORSES_PER_RACE else None
        if not race:
            log.append(f"{p['course']} : donnees indisponibles")
            continue
        by_num = {h["num"]: h for h in race["chevaux"]}
        try:
            adv = ask(PROMPT_BET.format(n=HORSES_PER_RACE, stake=STAKE, total=STAKE * HORSES_PER_RACE,
                                        heure=t.strftime("%H:%M"), top=len(race["chevaux"]),
                                        data=json.dumps(race, ensure_ascii=False, separators=(",", ":"))))
        except Exception as e:
            adv = {"pourquoi": "", "risques": "", "chevaux": [], "erreur": str(e)[:100]}
        picks, seen = [], set()
        for x in adv.get("chevaux", []):
            n = x.get("num")
            if n in by_num and n not in seen:
                picks.append((by_num[n], x)); seen.add(n)
        # complete avec les plus surs si Claude a mal repondu
        for h in sorted(race["chevaux"], key=lambda h: -h.get("p_place", 0)):
            if len(picks) >= HORSES_PER_RACE:
                break
            if h["num"] not in seen:
                picks.append((h, {})); seen.add(h["num"])
        picks = picks[:HORSES_PER_RACE]
        picks.sort(key=lambda hx: -float(hx[1].get("probabilite") or hx[0].get("p_place", 0)))
        mins = int((race["depart_ts"] / 1000 - t.timestamp()) // 60)
        nums = " · ".join(f"n°{h['num']} {h['nom']}" for h, _ in picks)
        for i, (h, x) in enumerate(picks):
            bet = {
                "id": f"{d.isoformat()}-{race['course']}-{h['num']}", "date": d.isoformat(),
                "groupe": f"{d.isoformat()}-{race['course']}",
                "course": race["course"], "r": race["r"], "c": race["c"], "nom_course": race["nom"],
                "hippodrome": race["hippodrome"], "depart": race["depart"], "depart_ts": race["depart_ts"],
                "discipline": race["discipline"], "partants": race["partants"],
                "places_payees": race["chevaux_payes_en_place"], "num": h["num"], "cheval": h["nom"],
                "cote": h["cote"], "type_pari": "SIMPLE_PLACE", "type_label": "Simple placé", "mise": STAKE,
                "probabilite": round(float(x.get("probabilite") or h.get("p_place", 0)), 2),
                "retour_min": round(h.get("rapport_min", 1.1) * STAKE, 2),
                "retour_max": round(h.get("rapport_max", 1.3) * STAKE, 2),
                "pourquoi": (x.get("pourquoi") or "") + (" " + adv.get("pourquoi", "") if i == 0 else ""),
                "risques": adv.get("risques", ""),
                "plan_b": f"Avec lui dans cette course : {nums}",
                "source": "claude" if x else "modele", "statut": "en_attente",
                "cree_le": t.isoformat(timespec="minutes"), "cree_ts": int(t.timestamp() * 1000), "final": True,
            }
            title = (f"⏰ Départ dans {mins} min · {race['course']} {race['hippodrome']} {race['depart']} · "
                     f"4 × SIMPLE PLACÉ {STAKE:.0f} € : " + ", ".join(str(hh["num"]) for hh, _ in picks))
            publish("bet", title, {"bet": bet, **({"silent": True} if i else {})}, priority=5 if i == 0 else 1)
        log.append(f"{race['course']} : 4 places publies ({nums})")
    return "; ".join(log)


def step_results(d, bets):
    log, ready = [], {}
    for b in bets.values():
        if b["statut"] != "en_attente" or now().timestamp() < b["depart_ts"] / 1000 + 8 * 60:
            continue
        j = get(f"{PMU}/{fmt(d)}/R{b['r']}/C{b['c']}/rapports-definitifs")
        if not j:
            continue
        rapport = None
        for p in j:
            if p["typePari"] in ("E_SIMPLE_PLACE", "SIMPLE_PLACE"):
                for r in p.get("rapports", []):
                    if str(b["num"]) in r["combinaison"].split("-"):
                        rapport = r["dividendePourUnEuro"] / 100
                if rapport:
                    break
        ps = partants(d, b["r"], b["c"], all_=True)
        me = next((p for p in ps if p["numPmu"] == b["num"]), {})
        arrivee = [p["numPmu"] for p in sorted((p for p in ps if p.get("ordreArrivee")), key=lambda p: p["ordreArrivee"])[:5]]
        if me.get("statut") == "NON_PARTANT":
            retour, verdict = b["mise"], "rembourse"
        elif rapport:
            retour, verdict = round(rapport * b["mise"], 2), "gagne"
        else:
            retour, verdict = 0.0, "perdu"
        ready.setdefault(b.get("groupe", b["id"]), []).append((b, {
            "bet_id": b["id"], "statut": verdict, "retour": retour, "gain": round(retour - b["mise"], 2),
            "rapport": rapport, "place": me.get("ordreArrivee"), "arrivee": arrivee}))
    for group, items in ready.items():
        mise = sum(b["mise"] for b, _ in items)
        retour = round(sum(r["retour"] for _, r in items), 2)
        payes = sum(r["statut"] == "gagne" for _, r in items)
        b0 = items[0][0]
        emoji = "✅" if retour > mise else ("↩️" if retour == mise else "❌")
        for i, (b, r) in enumerate(items):
            last = i == len(items) - 1
            title = (f"{emoji} {b0['course']} {b0['hippodrome']} : {payes}/{len(items)} placés · "
                     f"{retour:.2f} € pour {mise:.0f} € ({retour - mise:+.2f} €)") if last else f"{b['cheval']} : {r['statut']}"
            publish("result", title, {**r, **({} if last else {"silent": True})}, priority=4 if last else 1)
            b["statut"] = r["statut"]
            log.append(f"resultat {b['id']} : {r['statut']}")
    return log


PROMPT_QUINTE = """Voici le Quinté+ du jour, qui part dans environ {mins} minutes (données PMU en direct, {heure}).
{model}
Partants : numéro, nom, cote, musique, jockey/driver, entraîneur, gains de carrière, nb courses / victoires / places.
{data}

Donne TON pronostic final de 5 chevaux (ordre de préférence). Français simple, tutoiement, honnête sur l'incertitude.
Réponds UNIQUEMENT avec : {{"selection": [n1,n2,n3,n4,n5], "base": <n>, "outsider": <n>, "pourquoi": "<3 à 5 phrases>", "confiance": "<faible|moyenne|bonne>"}}"""


def step_quinte(d, quinte, model):
    if quinte:
        return "quinte deja conseille"
    for reunion, course in races(d, *WINDOW):
        if not any(p.get("typePari") in ("QUINTE_PLUS", "E_QUINTE_PLUS") for p in course.get("paris", [])):
            continue
        r, c = reunion["numOfficiel"], course["numOrdre"]
        ps = partants(d, r, c)
        data = [{"num": p["numPmu"], "nom": p["nom"], "cote": p["dernierRapportDirect"]["rapport"],
                 "musique": p.get("musique"), "jockey_driver": p.get("driver"), "entraineur": p.get("entraineur"),
                 "gains": (p.get("gainsParticipant") or {}).get("gainsCarriere"), "courses": p.get("nombreCourses"),
                 "victoires": p.get("nombreVictoires"), "places": p.get("nombrePlaces")} for p in ps]
        mtxt = ("Analyse statistique du logiciel de l'utilisateur (calculée sur son Chromebook) : "
                + json.dumps(model.get("resume"), ensure_ascii=False)) if model else ""
        mins = int((course["heureDepart"] / 1000 - now().timestamp()) // 60)
        adv = ask(PROMPT_QUINTE.format(mins=mins, heure=now().strftime("%H:%M"), model=mtxt,
                                       data=json.dumps(data, ensure_ascii=False, separators=(",", ":"))))
        t = now()
        q = {"date": d.isoformat(), "nom": course.get("libelle"), "hippodrome": reunion["hippodrome"]["libelleCourt"],
             "course": f"R{r}C{c}", "depart": start_of(course).strftime("%H:%M"), "depart_ts": course["heureDepart"],
             "selection": adv.get("selection"), "base": adv.get("base"), "outsider": adv.get("outsider"),
             "pourquoi": adv.get("pourquoi"), "confiance": adv.get("confiance"),
             "modele": (model or {}).get("pronostics"), "maj_le": t.strftime("%H:%M"), "cree_ts": int(t.timestamp() * 1000)}
        sel = "-".join(str(x) for x in q["selection"] or [])
        publish("quinte", f"⏰ Quinté+ dans {mins} min · {sel} · {q['course']} {q['hippodrome']} {q['depart']}", {"quinte": q}, priority=5)
        return f"quinte publie : {sel}"
    return "pas de quinte dans la fenetre"


def norm(x):
    import unicodedata
    x = unicodedata.normalize("NFD", str(x).lower())
    return "".join(ch for ch in x if ch.isalnum() or ch == " ")


def programme_chat(d, question):
    """Toutes les courses (tous pays) des 5 prochaines heures, en compact,
    + le detail complet des courses citees (RxCy, ou hippodrome et/ou heure ; fautes de frappe tolerees)."""
    import difflib
    import re
    q = norm(question)
    mots = [w for w in q.split() if len(w) >= 4]
    rc = {(int(a), int(b)) for a, b in re.findall(r"r\s*(\d+)\s*c\s*(\d+)", q)}
    heures = {f"{int(h):02d}:{m}" for h, m in re.findall(r"(\d{1,2})\s*[h:]\s*(\d{2})", question.lower())}
    liste, scored = [], []
    for reunion, course in races(d, -30, 300, all_countries=True):
        r, c = reunion["numOfficiel"], course["numOrdre"]
        hippo = reunion["hippodrome"]["libelleCourt"]
        dep = start_of(course).strftime("%H:%M")
        liste.append(f"R{r}C{c} {hippo} ({reunion.get('pays', {}).get('code')}) {dep} {course.get('discipline', '')}")
        h_hit = any(difflib.get_close_matches(w, mots, n=1, cutoff=0.8) for w in norm(hippo).split() if len(w) >= 4)
        score = 3 * ((r, c) in rc) + 2 * (dep in heures) + h_hit
        if score >= 2 or (h_hit and not heures and not rc):
            scored.append((-score, course["heureDepart"], r, c, hippo, dep, reunion, course))
    detail = []
    for _, _, r, c, hippo, dep, reunion, course in sorted(scored, key=lambda x: x[:2])[:2]:
        ps = partants(d, r, c, all_=True)
        detail.append({
            "course": f"R{r}C{c}", "hippodrome": hippo, "pays": reunion.get("pays", {}).get("code"),
            "depart": dep, "nom": course.get("libelle"), "discipline": course.get("discipline"),
            "distance_m": course.get("distance"),
            "places_payees": nb_places(len([p for p in ps if p.get("statut") == "PARTANT"])),
            "chevaux": [{"num": p["numPmu"], "nom": p["nom"], "statut": p.get("statut"),
                         "cote": (p.get("dernierRapportDirect") or p.get("dernierRapportReference") or {}).get("rapport"),
                         "musique": p.get("musique"), "jockey": p.get("driver"),
                         "entraineur": p.get("entraineur")} for p in ps],
        })
    return liste, detail


GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-flash-latest", "gemini-flash-lite-latest")


def day_lines(day, with_arrival=False):
    """Programme compact d'un autre jour (tous pays)."""
    prog = (get(f"{PMU}/{fmt(day)}") or {}).get("programme", {})
    out = []
    for reunion in prog.get("reunions", []):
        for course in reunion.get("courses", []):
            quinte = any("QUINTE" in p.get("typePari", "") for p in course.get("paris", []))
            arr = "-".join(str(x[0]) for x in (course.get("ordreArrivee") or [])[:5]) if with_arrival else ""
            out.append(f"R{reunion['numOfficiel']}C{course['numOrdre']} {reunion['hippodrome']['libelleCourt']} "
                       f"({reunion.get('pays', {}).get('code')}) {start_of(course).strftime('%H:%M')} {course.get('discipline', '')}"
                       f" {course.get('nombreDeclaresPartants', '')}p" + (" QUINTE+" if quinte else "") + (f" arrivee {arr}" if arr else ""))
    return out


def other_days(d, question):
    q = norm(question)
    days = {}
    if "apres demain" in q or "apresdemain" in q:
        days["apres_demain"] = (d + dt.timedelta(days=2), False)
    elif "demain" in q:
        days["demain"] = (d + dt.timedelta(days=1), False)
    if "hier" in q:
        days["hier"] = (d - dt.timedelta(days=1), True)
    return {k: day_lines(v[0], v[1]) for k, v in days.items()}


def gemini_chat(system, prompt):
    if not GEMINI_KEY:
        raise RuntimeError("pas de cle Gemini")
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.4}}
    err = ""
    for model in GEMINI_MODELS:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=90)
            if r.status_code == 200:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if text:
                    return text
            err = f"{model} {r.status_code}"
        except Exception as e:
            err = f"{model} {e}"
    raise RuntimeError(err)


def chat_topic(box):
    return f"{TOPIC}-c-{box}"


def step_chats(d, bets, quinte, chat, boxes):
    """Chaque compte a son canal de chat prive ; l'ancien chat commun reste gere pour les vieilles versions."""
    log = [step_chat(d, bets, quinte, chat)]
    for box in sorted(boxes):
        topic = chat_topic(box)
        msgs = [{**m["body"], "time": m["time"]} for m in read_channel(topic)
                if m["body"].get("kind") in ("chat_q", "chat_a")]
        log.append(f"{box[:4]}…: {step_chat(d, bets, quinte, msgs, topic)}")
    return " | ".join(log)


def step_chat(d, bets, quinte, chat, topic=None):
    answered = {m.get("reply_to") for m in chat if m.get("kind") == "chat_a"}
    pending = [m for m in chat if m.get("kind") == "chat_q" and m.get("id") not in answered]
    if not pending:
        return "aucun message"
    last = pending[-1]
    ai = last.get("ai") or "claude"
    who = lambda m: "Utilisateur" if m["kind"] == "chat_q" else ("Gemini" if m.get("ai") == "gemini" else "Claude")
    # « Effacer le chat » dans l'appli : on oublie tout ce qui precede (since = ms)
    since = max(now().timestamp() - 12 * 3600, (last.get("since") or 0) / 1000)
    drop = set(last.get("drop") or [])  # messages supprimes un par un dans l'appli
    gone = lambda m: (m.get("id") if m.get("kind") == "chat_q" else "a:" + str(m.get("reply_to"))) in drop
    history = [m for m in chat if m.get("time", 0) > since and not gone(m)][-24:]
    lines = [f"{who(m)} : {m.get('text', '')}" for m in history]
    # ntfy ne garde que 12 h : l'appli joint les derniers messages qu'elle a en memoire
    earlier = [f"{'Utilisateur' if h.get('u') else h.get('ai', 'Claude').capitalize()} : {h.get('t', '')}"
               for h in (last.get("hist") or [])]
    if len(history) <= len(pending) and earlier:
        lines = earlier + lines
    convo = "\n".join(lines)
    question = last.get("text", "")
    liste, detail = programme_chat(d, question)
    ctx = {
        "date": d.isoformat(), "heure": now().strftime("%H:%M"),
        "programme_5h_tous_pays": liste,
        "courses_citees_detail": detail,
        "paris_conseilles_aujourdhui": [{k: b.get(k) for k in ("course", "hippodrome", "depart", "num", "cheval", "type_label",
                                                                 "probabilite", "statut", "pourquoi")} for b in bets.values()],
        "quinte": quinte,
        "favoris_solides_fr_3h": candidates(d, 5, 180, max_races=6),
        **{f"programme_{k}": v for k, v in other_days(d, question).items()},
    }
    me = "Gemini" if ai == "gemini" else "Claude"
    prompt = (f"Contexte (données PMU en direct) :\n{json.dumps(ctx, ensure_ascii=False, separators=(',', ':'))}\n\n"
              f"Conversation (l'utilisateur alterne entre Claude et Gemini : c'est UNE seule conversation, "
              f"reprends-la naturellement, même les réponses de l'autre IA) :\n{convo}\n\n"
              f"Tu es {me}. Réponds au dernier message de l'utilisateur. Si tu conseilles un pari, "
              "indique toujours le type de jeu, la course (RxCy, hippodrome, heure) et le cheval (numéro + nom).")
    system = SYSTEM_CHAT.replace("Tu es Claude", f"Tu es {me}")
    text = None
    if ai == "gemini":
        try:
            text = gemini_chat(system, prompt)
        except Exception as e:
            print(f"gemini: {e}")
            ai = "claude"
            prompt = prompt.replace("Tu es Gemini.", "Tu es Claude (Gemini est indisponible, dis-le en une courte phrase).")
    if text is None:
        try:
            text = ask(prompt, system=SYSTEM_CHAT + WEB_CHAT, as_json=False, tools="WebSearch,WebFetch", turns=16)
        except Exception as e:  # sans reponse, la question serait reposee a chaque passage
            print(f"chat: {e}")
            text = "Désolé, je n'ai pas pu répondre cette fois (erreur du serveur). Repose ta question dans une minute."
    publish("chat_a", f"💬 {'Gemini' if ai == 'gemini' else 'Claude'} : " + text[:80],
            {"reply_to": last["id"], "text": text[:3500], "ai": ai, "ts": int(now().timestamp() * 1000)}, priority=4, topic=topic)
    return f"reponse envoyee ({ai})"


def main(mode):
    if not TOPIC:
        sys.exit("NTFY_TOPIC manquant")
    d = now().date()
    if mode == "candidates":
        print(json.dumps(candidates(d, 5, 360), ensure_ascii=False, indent=1))
        return
    bets, quinte, chat, model, boxes, plan = state()
    log = []
    for name, fn in (("chat", lambda: step_chats(d, bets, quinte, chat, boxes)),
                     ("resultats", lambda: step_results(d, bets)),
                     ("plan", lambda: step_plan(d, plan, bets) if mode == "tick" else "-"),
                     ("pari", lambda: step_bet(d, bets, plan) if mode == "tick" else "-"),
                     ("quinte", lambda: step_quinte(d, quinte, model) if mode == "tick" else "-")):
        try:
            log.append(f"{name}: {fn()}")
        except Exception as e:  # une etape en echec ne bloque pas les autres
            log.append(f"{name}: ERREUR {e}")
    print("\n".join(log))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tick")
