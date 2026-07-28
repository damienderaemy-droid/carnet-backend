"""
Étape 2 — Vraies données Switzerland Tourism
==============================================
Remplace les données factices par un vrai appel à l'API ST OpenData,
avec la clé stockée en variable d'environnement (jamais dans le code).
"""

import os
import requests
from fastapi import FastAPI, Query

app = FastAPI(title="Carnet - API")

ST_API_KEY = os.environ.get("ST_API_KEY", "")
ST_BASE_URL = "https://opendata.myswitzerland.io/v1"

# Traduction des codes de genre de l'API vers nos propres catégories
GENRE_LABELS = {
    "nature": "Nature",
    "adventure": "Aventure",
    "active": "Actif",
    "education": "Découverte",
    "culture": "Culture",
    "culinary": "Culinaire",
    "relax": "Détente",
}


def appeler_api_st(endpoint: str, params: dict):
    headers = {"x-api-key": ST_API_KEY, "Accept": "application/json"}
    params.setdefault("lang", "fr")
    resp = requests.get(f"{ST_BASE_URL}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extraire_genre(classification: list) -> str:
    """Prend le premier genre reconnu trouvé dans la classification brute."""
    for c in classification or []:
        if c.get("name") == "experiencetype":
            for v in c.get("values", []):
                code = v.get("name")
                if code in GENRE_LABELS:
                    return code
    return "autre"


@app.get("/")
def accueil():
    return {"statut": "ok", "message": "Le backend fonctionne !"}


@app.get("/activites")
def activites(recherche: str = Query("Sion", description="Ville ou région à chercher")):
    """Renvoie des activités réelles depuis Switzerland Tourism, simplifiées."""
    if not ST_API_KEY:
        return {"erreur": "Clé API manquante — vérifie la variable ST_API_KEY sur Railway."}

    data = appeler_api_st("attractions", {"query": recherche, "expand": "false"})
    resultats = []

    for item in data.get("data", []):
        resultats.append({
            "nom": item.get("name"),
            "resume": item.get("abstract"),
            "genre": extraire_genre(item.get("classification")),
        })

    return {"recherche": recherche, "total": len(resultats), "activites": resultats}
