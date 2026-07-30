"""
Étape 4 — Authentification réelle (Clerk)
============================================
Ajoute une vérification de connexion et une table pour stocker les goûts
de chaque utilisateur, liés à son vrai compte (pas au navigateur).
"""

import os
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Query, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="Carnet - API")

# Autorise le frontend à appeler ce backend depuis n'importe quel domaine.
# Pour l'instant "*" (tout autoriser) le temps des tests — à restreindre
# au(x) vrai(s) domaine(s) de l'appli une fois en production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ST_API_KEY = os.environ.get("ST_API_KEY", "")
ST_BASE_URL = "https://opendata.myswitzerland.io/v1"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
CLERK_JWKS_URL = os.environ.get("CLERK_JWKS_URL", "")

GENRE_LABELS = {
    "nature": "Nature", "adventure": "Aventure", "active": "Actif",
    "education": "Découverte", "culture": "Culture",
    "culinary": "Culinaire", "relax": "Détente",
}

_jwks_client = None


def get_jwks_client():
    """Le client JWKS est mis en cache pour ne pas le recréer à chaque appel."""
    global _jwks_client
    if _jwks_client is None and CLERK_JWKS_URL:
        _jwks_client = PyJWKClient(CLERK_JWKS_URL)
    return _jwks_client


def verifier_connexion(authorization: str = Header(default=None)) -> str:
    """Vérifie le jeton envoyé par le frontend et renvoie l'identifiant Clerk
    de la personne connectée. Bloque la requête si le jeton est absent/invalide."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Non connecté.")

    jeton = authorization.removeprefix("Bearer ").strip()
    client = get_jwks_client()
    if client is None:
        raise HTTPException(status_code=500, detail="Authentification mal configurée côté serveur.")

    try:
        cle_signature = client.get_signing_key_from_jwt(jeton)
        contenu = jwt.decode(jeton, cle_signature.key, algorithms=["RS256"], options={"verify_aud": False})
        return contenu["sub"]  # identifiant unique et stable de la personne, fourni par Clerk
    except Exception as e:
        print(f"ERREUR DE VÉRIFICATION DU JETON : {type(e).__name__}: {e}")
        raise HTTPException(status_code=401, detail=f"Session invalide : {e}")


def get_connexion():
    return psycopg2.connect(DATABASE_URL)


def initialiser_base():
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activites (
                    id SERIAL PRIMARY KEY,
                    recherche TEXT NOT NULL,
                    nom TEXT, resume TEXT, genre TEXT, duree TEXT, source_url TEXT,
                    recupere_le TIMESTAMPTZ NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS preferences (
                    utilisateur_id TEXT NOT NULL,
                    genre TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    maj_le TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (utilisateur_id, genre)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS profils (
                    utilisateur_id TEXT PRIMARY KEY,
                    prenom TEXT, commune TEXT, age TEXT, mode TEXT,
                    maj_le TIMESTAMPTZ NOT NULL
                );
            """)
            # Si la table 'activites' existait déjà sans ces colonnes (versions précédentes)
            cur.execute("ALTER TABLE activites ADD COLUMN IF NOT EXISTS source_url TEXT;")
            cur.execute("ALTER TABLE activites ADD COLUMN IF NOT EXISTS duree TEXT;")
        conn.commit()


def appeler_api_st(endpoint: str, params: dict):
    headers = {"x-api-key": ST_API_KEY, "Accept": "application/json"}
    params.setdefault("lang", "fr")
    resp = requests.get(f"{ST_BASE_URL}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


DUREE_LABELS = {
    "lessthan1hour": "moins d'1h", "2to4hourshalfday": "2 à 4h",
    "between12hours": "1 à 2h", "4to8hoursfullday": "demi-journée",
    "7days": "plusieurs jours",
}


def extraire_genre(classification: list) -> str:
    for c in classification or []:
        if c.get("name") == "experiencetype":
            for v in c.get("values", []):
                code = v.get("name")
                if code in GENRE_LABELS:
                    return code
    return "autre"


def extraire_duree(classification: list) -> str:
    for c in classification or []:
        if c.get("name") == "neededtime":
            for v in c.get("values", []):
                return DUREE_LABELS.get(v.get("name"), "variable")
    return "variable"


def rafraichir_depuis_api(recherche: str):
    data = appeler_api_st("attractions", {"query": recherche, "expand": "false"})
    maintenant = datetime.now(timezone.utc)
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM activites WHERE recherche = %s", (recherche,))
            for item in data.get("data", []):
                classification = item.get("classification")
                cur.execute(
                    "INSERT INTO activites (recherche, nom, resume, genre, duree, source_url, recupere_le) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (recherche, item.get("name"), item.get("abstract"),
                     extraire_genre(classification), extraire_duree(classification),
                     item.get("links", {}).get("self"), maintenant)
                )
        conn.commit()


def lire_depuis_base(recherche: str):
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nom, resume, genre, duree, source_url, recupere_le FROM activites WHERE recherche = %s", (recherche,))
            lignes = cur.fetchall()
    if not lignes:
        return [], False
    _, _, _, _, _, recupere_le = lignes[0]
    fraiche = (datetime.now(timezone.utc) - recupere_le) < timedelta(hours=24)
    # NOTE : le lieu précis (commune exacte) n'est pas fiable sans un appel détaillé
    # supplémentaire par activité — on utilise donc le terme de recherche comme lieu
    # approximatif ("Sion et environs"), pas la localité exacte de chaque item.
    return [{"nom": n, "resume": r, "genre": g, "duree": d, "lieu": f"{recherche} et environs", "source_url": u}
            for
