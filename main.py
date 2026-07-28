"""
Étape 3 — Vraie base de données
=================================
Au lieu d'interroger l'API Switzerland Tourism à chaque visite, on stocke
les résultats dans PostgreSQL et on les ressert depuis là. On ne rappelle
l'API que si la base est vide, ou si les données ont plus de 24h.
"""

import os
import json
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from fastapi import FastAPI, Query

app = FastAPI(title="Carnet - API")

ST_API_KEY = os.environ.get("ST_API_KEY", "")
ST_BASE_URL = "https://opendata.myswitzerland.io/v1"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

GENRE_LABELS = {
    "nature": "Nature", "adventure": "Aventure", "active": "Actif",
    "education": "Découverte", "culture": "Culture",
    "culinary": "Culinaire", "relax": "Détente",
}


def get_connexion():
    return psycopg2.connect(DATABASE_URL)


def initialiser_base():
    """Crée la table si elle n'existe pas encore. Sans danger si déjà créée."""
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activites (
                    id SERIAL PRIMARY KEY,
                    recherche TEXT NOT NULL,
                    nom TEXT,
                    resume TEXT,
                    genre TEXT,
                    recupere_le TIMESTAMPTZ NOT NULL
                );
            """)
        conn.commit()


def appeler_api_st(endpoint: str, params: dict):
    headers = {"x-api-key": ST_API_KEY, "Accept": "application/json"}
    params.setdefault("lang", "fr")
    resp = requests.get(f"{ST_BASE_URL}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extraire_genre(classification: list) -> str:
    for c in classification or []:
        if c.get("name") == "experiencetype":
            for v in c.get("values", []):
                code = v.get("name")
                if code in GENRE_LABELS:
                    return code
    return "autre"


def rafraichir_depuis_api(recherche: str):
    """Va chercher les données fraîches et les enregistre dans la base."""
    data = appeler_api_st("attractions", {"query": recherche, "expand": "false"})
    maintenant = datetime.now(timezone.utc)

    with get_connexion() as conn:
        with conn.cursor() as cur:
            # On supprime l'ancien cache pour cette recherche avant d'insérer le nouveau
            cur.execute("DELETE FROM activites WHERE recherche = %s", (recherche,))
            for item in data.get("data", []):
                cur.execute(
                    "INSERT INTO activites (recherche, nom, resume, genre, recupere_le) VALUES (%s,%s,%s,%s,%s)",
                    (recherche, item.get("name"), item.get("abstract"),
                     extraire_genre(item.get("classification")), maintenant)
                )
        conn.commit()


def lire_depuis_base(recherche: str):
    """Renvoie (activités, sont-elles fraîches ?) depuis la base."""
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nom, resume, genre, recupere_le FROM activites WHERE recherche = %s",
                (recherche,)
            )
            lignes = cur.fetchall()

    if not lignes:
        return [], False

    _, _, _, recupere_le = lignes[0]
    fraiche = (datetime.now(timezone.utc) - recupere_le) < timedelta(hours=24)
    activites = [{"nom": n, "resume": r, "genre": g} for n, r, g, _ in lignes]
    return activites, fraiche


@app.on_event("startup")
def au_demarrage():
    initialiser_base()


@app.get("/")
def accueil():
    return {"statut": "ok", "message": "Le backend fonctionne !"}


@app.get("/activites")
def activites(recherche: str = Query("Sion")):
    if not ST_API_KEY:
        return {"erreur": "Clé API manquante."}

    resultats, fraiche = lire_depuis_base(recherche)

    if not fraiche:
        rafraichir_depuis_api(recherche)
        resultats, _ = lire_depuis_base(recherche)

    return {"recherche": recherche, "total": len(resultats), "activites": resultats, "servi_depuis": "base de données"}
