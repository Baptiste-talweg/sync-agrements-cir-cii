"""
Synchronisation des agréments CIR / CII (open data MESR).

1. Télécharge les deux jeux de données depuis le portail du ministère.
2. Regroupe toutes les lignes par organisme (ou personne) : une fiche par entité.
3. Compare avec la version précédente (data/etat.json).
4. Écrit :
   - data/agrements.csv     : liste complète (pour l'import initial dans Notion)
   - data/changements.json  : uniquement les créations / mises à jour / retraits
   - data/etat.json         : la mémoire pour la prochaine comparaison

Uniquement la bibliothèque standard Python : rien à installer.
"""
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data.enseignementsup-recherche.gouv.fr/api/explore/v2.1/catalog/datasets"
SOURCES = {
    "CIR": "organismes-experts-bureaux-2-style-stylistes-agrees-credit-d-impot-recherche-cir",
    "CII": "organismes-prives-agrees-credit-dimpot-innovation-cii",
}
EXPORT = "/exports/csv?delimiter=%3B&use_labels=true"

DATA = Path(__file__).resolve().parent.parent / "data"
ETAT = DATA / "etat.json"
CSV_SORTIE = DATA / "agrements.csv"
CHANGEMENTS = DATA / "changements.json"

COLONNES_REQUISES = [
    "Dispositif", "Type de structure", "Année d'agrément", "Type", "Numéro SIREN",
    "Désignation", "Sigle", "Activité", "Début d'agrément", "Fin d'agrément",
    "Base SIRENE : Catégorie de l'établissement siège",
    "Base SIRENE : Code APE de l'établissement siège",
    "Ville", "Département", "Région", "Pays",
]

TYPES_COURTS = {
    "Organismes de recherche agréés": "Organisme de recherche (CIR)",
    "Experts scientifiques ou techniques": "Expert scientifique (CIR)",
    "Organismes privés agréés": "Organisme privé (CII)",
}

# Ordre des colonnes = noms des propriétés Notion
CHAMPS = [
    "Nom", "Clé", "SIREN", "Sigle", "Dispositifs", "Types de structure", "Nature",
    "Années CIR", "Années CII", "Première année", "Fin CIR", "Fin CII",
    "Activité", "Catégorie entreprise", "Code APE", "Ville", "Département",
    "Région", "Pays", "Fiche entreprise",
]


def lire_source(dispositif, chemin_local=None):
    if chemin_local:
        texte = Path(chemin_local).read_text(encoding="utf-8-sig")
    else:
        url = BASE + "/" + SOURCES[dispositif] + EXPORT
        print(f"Téléchargement {dispositif} : {url}")
        with urllib.request.urlopen(url, timeout=300) as r:
            texte = r.read().decode("utf-8-sig")
    lignes = list(csv.DictReader(io.StringIO(texte), delimiter=";"))
    if not lignes:
        sys.exit(f"ERREUR : le fichier {dispositif} est vide.")
    manquantes = [c for c in COLONNES_REQUISES if c not in lignes[0]]
    if manquantes:
        sys.exit(f"ERREUR : colonnes absentes dans {dispositif} : {manquantes}. "
                 "Le format de la source a changé, le script doit être adapté.")
    print(f"  {len(lignes)} lignes lues pour {dispositif}")
    return lignes


def normaliser(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]+", " ", txt.upper()).strip()


def cle(ligne):
    siren = (ligne.get("Numéro SIREN") or "").strip()
    if siren:
        return "SIREN-" + siren
    return "NOM-" + normaliser(ligne.get("Désignation")) + "|" + normaliser(ligne.get("Pays"))


def annees(txt):
    return {int(a) for a in re.findall(r"\d{4}", txt or "")}


def entier(txt):
    try:
        return int(float(txt))
    except (TypeError, ValueError):
        return None


def regrouper(lignes):
    entites = {}
    for l in lignes:
        k = cle(l)
        e = entites.setdefault(k, {"lignes": [], "annees": {"CIR": set(), "CII": set()}})
        e["lignes"].append(l)
        disp = l["Dispositif"].strip().upper()
        if disp in e["annees"]:
            e["annees"][disp] |= annees(l["Année d'agrément"])
    fiches = {}
    for k, e in entites.items():
        # Les infos descriptives viennent de l'agrément le plus récent
        recente = max(e["lignes"], key=lambda l: entier(l["Fin d'agrément"]) or 0)
        siren = (recente.get("Numéro SIREN") or "").strip()
        a_cir, a_cii = sorted(e["annees"]["CIR"]), sorted(e["annees"]["CII"])
        types = sorted({TYPES_COURTS.get(l["Type de structure"], l["Type de structure"])
                        for l in e["lignes"]})
        dispositifs = sorted({l["Dispositif"].strip().upper() for l in e["lignes"]})
        toutes = a_cir + a_cii
        fiches[k] = {
            "Nom": recente["Désignation"].strip(),
            "Clé": k,
            "SIREN": siren,
            "Sigle": (recente.get("Sigle") or "").strip(),
            "Dispositifs": ", ".join(dispositifs),
            "Types de structure": ", ".join(types),
            "Nature": recente["Type"].strip(),
            "Années CIR": ", ".join(map(str, a_cir)),
            "Années CII": ", ".join(map(str, a_cii)),
            "Première année": min(toutes) if toutes else "",
            "Fin CIR": max(a_cir) if a_cir else "",
            "Fin CII": max(a_cii) if a_cii else "",
            "Activité": (recente.get("Activité") or "").strip(),
            "Catégorie entreprise": (recente.get("Base SIRENE : Catégorie de l'établissement siège") or "").strip(),
            "Code APE": (recente.get("Base SIRENE : Code APE de l'établissement siège") or "").strip(),
            "Ville": (recente.get("Ville") or "").strip(),
            "Département": (recente.get("Département") or "").strip(),
            "Région": (recente.get("Région") or "").strip(),
            "Pays": (recente.get("Pays") or "").strip(),
            "Fiche entreprise": f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}" if siren else "",
        }
    return fiches


def empreinte(fiche):
    return hashlib.md5(json.dumps(fiche, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    locaux = dict(a.split("=", 1) for a in sys.argv[1:])  # test : CIR=fichier.csv CII=fichier.csv
    lignes = []
    for d in SOURCES:
        lignes += lire_source(d, locaux.get(d))
    fiches = regrouper(lignes)
    print(f"{len(fiches)} fiches après regroupement")

    DATA.mkdir(exist_ok=True)
    with CSV_SORTIE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CHAMPS)
        w.writeheader()
        for k in sorted(fiches):
            w.writerow(fiches[k])

    nouvel_etat = {k: empreinte(v) for k, v in fiches.items()}
    premier_run = not ETAT.exists()
    ancien_etat = {} if premier_run else json.loads(ETAT.read_text(encoding="utf-8"))

    changements = []
    if not premier_run:
        for k, h in nouvel_etat.items():
            if k not in ancien_etat:
                changements.append({"action": "creation", **fiches[k]})
            elif ancien_etat[k] != h:
                changements.append({"action": "mise_a_jour", **fiches[k]})
        for k in ancien_etat:
            if k not in nouvel_etat:
                changements.append({"action": "retrait", "Clé": k})

    if changements:
        contenu = {"lot": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "nombre": len(changements), "changements": changements}
    else:
        contenu = {"lot": None, "nombre": 0, "changements": []}
    CHANGEMENTS.write_text(json.dumps(contenu, ensure_ascii=False, indent=1), encoding="utf-8")
    ETAT.write_text(json.dumps(nouvel_etat, sort_keys=True, indent=0), encoding="utf-8")

    if premier_run:
        print("Premier passage : état initial créé, aucun changement envoyé à Make. "
              "Importe data/agrements.csv dans Notion.")
    else:
        resume = {a: sum(1 for c in changements if c["action"] == a)
                  for a in ("creation", "mise_a_jour", "retrait")}
        print(f"Changements : {resume}")


if __name__ == "__main__":
    main()
