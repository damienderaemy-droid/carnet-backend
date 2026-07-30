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
EVENTFROG_API_KEY = os.environ.get("EVENTFROG_API_KEY", "")

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
                    lat DOUBLE PRECISION, lon DOUBLE PRECISION,
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
                    prenom TEXT, nom TEXT, commune TEXT, age TEXT, mode TEXT,
                    maj_le TIMESTAMPTZ NOT NULL
                );
            """)
            cur.execute("ALTER TABLE profils ADD COLUMN IF NOT EXISTS nom TEXT;")
            # Si la table 'activites' existait déjà sans ces colonnes (versions précédentes)
            cur.execute("ALTER TABLE activites ADD COLUMN IF NOT EXISTS source_url TEXT;")
            cur.execute("ALTER TABLE activites ADD COLUMN IF NOT EXISTS duree TEXT;")
            cur.execute("ALTER TABLE activites ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;")
            cur.execute("ALTER TABLE activites ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION;")
        conn.commit()


import math

def geocoder_commune(nom: str):
    """Trouve les coordonnées GPS d'une commune suisse via l'API officielle
    geo.admin.ch (gratuite, sans clé). Renvoie (lat, lon) ou None si introuvable."""
    try:
        resp = requests.get(
            "https://api3.geo.admin.ch/rest/services/api/SearchServer",
            params={"searchText": nom, "type": "locations", "limit": 1, "sr": 4326},
            timeout=8,
        )
        resp.raise_for_status()
        resultats = resp.json().get("results", [])
        if not resultats:
            return None
        attrs = resultats[0].get("attrs", {})
        lat, lon = attrs.get("lat"), attrs.get("lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception as e:
        print(f"Géocodage impossible pour '{nom}' : {e}")
        return None


def distance_km(lat1, lon1, lat2, lon2):
    """Distance à vol d'oiseau entre deux points GPS (formule de Haversine)."""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))


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


POOL_KEY = "Valais"  # on interroge tout le canton une fois, puis on filtre par distance réelle


def rafraichir_depuis_api(recherche: str = POOL_KEY):
    """Récupère un large pool d'activités valaisannes (plusieurs pages),
    avec leurs coordonnées GPS réelles pour un filtrage par distance ensuite."""
    maintenant = datetime.now(timezone.utc)
    vus = {}
    for page in range(5):  # jusqu'à 5 pages de 50 = 250 activités environ
        data = appeler_api_st("attractions", {"query": POOL_KEY, "expand": "false", "page": page, "hitsPerPage": 50})
        items = data.get("data", [])
        if not items:
            break
        for item in items:
            vus[item.get("identifier")] = item

    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM activites WHERE recherche = %s", (POOL_KEY,))
            for item in vus.values():
                classification = item.get("classification")
                geo = item.get("geo") or {}
                cur.execute(
                    "INSERT INTO activites (recherche, nom, resume, genre, duree, source_url, lat, lon, recupere_le) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (POOL_KEY, item.get("name"), item.get("abstract"),
                     extraire_genre(classification), extraire_duree(classification),
                     item.get("links", {}).get("self"), geo.get("latitude"), geo.get("longitude"),
                     maintenant)
                )
        conn.commit()


def lire_depuis_base(recherche: str = POOL_KEY):
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nom, resume, genre, duree, source_url, lat, lon, recupere_le FROM activites WHERE recherche = %s", (POOL_KEY,))
            lignes = cur.fetchall()
    if not lignes:
        return [], False
    recupere_le = lignes[0][7]
    fraiche = (datetime.now(timezone.utc) - recupere_le) < timedelta(hours=24)
    return [{"nom": n, "resume": r, "genre": g, "duree": d, "source_url": u, "lat": la, "lon": lo}
            for n, r, g, d, u, la, lo, _ in lignes], fraiche


@app.on_event("startup")
def au_demarrage():
    initialiser_base()


@app.get("/")
def page_application():
    """Sert directement l'appli à l'adresse racine — plus simple à partager,
    plus aucun risque de confusion avec le message technique."""
    return FileResponse("index.html")


@app.get("/status")
def statut_technique():
    """Vérification technique que le serveur tourne (ancien contenu de '/')."""
    return {"statut": "ok", "message": "Le backend fonctionne !"}


@app.get("/app")
def page_application_alias():
    """Ancien lien, gardé pour compatibilité avec ce qui a déjà été partagé."""
    return FileResponse("index.html")


@app.get("/img-{nom}.png")
def servir_image(nom: str):
    """Sert n'importe quelle image d'illustration (img-nature.png, img-adventure.png, etc.)
    sans avoir à ajouter une route à chaque nouvelle image."""
    return FileResponse(f"img-{nom}.png")


@app.get("/activites")
def activites(recherche: str = Query("Sion"), rayon_km: float = Query(20.0)):
    if not ST_API_KEY:
        return {"erreur": "Clé API manquante."}

    pool, fraiche = lire_depuis_base()
    if not fraiche:
        rafraichir_depuis_api()
        pool, _ = lire_depuis_base()

    centre = geocoder_commune(recherche)
    if centre is None:
        # Géocodage impossible : on renvoie le pool complet, sans filtrage par distance,
        # plutôt que de bloquer complètement la recherche.
        resultats = [{**a, "lieu": "Valais", "distance_km": None} for a in pool]
        return {"recherche": recherche, "rayon_km": rayon_km, "total": len(resultats),
                "activites": resultats, "avertissement": "Commune non géolocalisée, distance non calculée."}

    lat_c, lon_c = centre
    resultats = []
    for a in pool:
        if a["lat"] is None or a["lon"] is None:
            continue
        d = distance_km(lat_c, lon_c, a["lat"], a["lon"])
        if d <= rayon_km:
            resultats.append({**a, "lieu": f"à {round(d)} km de {recherche}", "distance_km": round(d, 1)})

    resultats.sort(key=lambda a: a["distance_km"])
    return {"recherche": recherche, "rayon_km": rayon_km, "total": len(resultats), "activites": resultats}


@app.get("/mon-profil")
def lire_profil(utilisateur_id: str = Depends(verifier_connexion)):
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT prenom, nom, commune, age, mode FROM profils WHERE utilisateur_id = %s", (utilisateur_id,))
            ligne = cur.fetchone()
    if not ligne:
        return {"existe": False}
    prenom, nom, commune, age, mode = ligne
    return {"existe": True, "prenom": prenom, "nom": nom, "commune": commune, "age": age, "mode": mode}


@app.post("/mon-profil")
def maj_profil(prenom: str, nom: str, commune: str, age: str, mode: str, utilisateur_id: str = Depends(verifier_connexion)):
    maintenant = datetime.now(timezone.utc)
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO profils (utilisateur_id, prenom, nom, commune, age, mode, maj_le)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (utilisateur_id)
                DO UPDATE SET prenom = %s, nom = %s, commune = %s, age = %s, mode = %s, maj_le = %s
            """, (utilisateur_id, prenom, nom, commune, age, mode, maintenant,
                  prenom, nom, commune, age, mode, maintenant))
        conn.commit()
    return {"statut": "ok"}


@app.get("/test-eventfrog")
def test_eventfrog():
    """Endpoint de TEST uniquement — vérifie ce qu'Eventfrog renvoie pour le
    Valais. À retirer avant tout vrai lancement, ce n'est pas fait pour durer."""
    if not EVENTFROG_API_KEY:
        return {"erreur": "Clé EVENTFROG_API_KEY manquante sur Railway."}

    # Codes postaux valaisans (Bas-Valais 1870-1998, Haut-Valais 3900-3999).
    # Plus précis qu'un rayon en km, qui déborde sur les cantons voisins
    # vu la forme tout en longueur du Valais.
    npa_valais = [str(n) for n in list(range(1870, 1999)) + list(range(3900, 4000))]

    headers = {"Authorization": f"Bearer {EVENTFROG_API_KEY}"}
    params = {"zip": npa_valais, "country": "CH", "perPage": 50}

    try:
        resp = requests.get("https://api.eventfrog.net/public/v1/events", headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"erreur": f"{type(e).__name__}: {e}"}

    evenements = []
    for ev in data.get("events", []):
        titre_dict = ev.get("title") or {}
        titre = titre_dict.get("fr") or titre_dict.get("de") or titre_dict.get("en") or titre_dict.get("it") \
            or next(iter(titre_dict.values()), "?")
        evenements.append({
            "titre": titre,
            "debut": ev.get("begin"),
            "fin": ev.get("end"),
            "prix_min": ev.get("lowestTicketPrice"),
            "lien": ev.get("url"),
        })

    return {"total_disponible": data.get("totalNumberOfResources"), "recus": len(evenements), "evenements": evenements}


@app.get("/mes-gouts")
def lire_mes_gouts(utilisateur_id: str = Depends(verifier_connexion)):
    """Renvoie les scores de goûts de la personne connectée."""
    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT genre, score FROM preferences WHERE utilisateur_id = %s",
                (utilisateur_id,)
            )
            lignes = cur.fetchall()
    return {"gouts": {genre: score for genre, score in lignes}}


@app.post("/mes-gouts/{genre}")
def maj_gout(genre: str, aime: bool, utilisateur_id: str = Depends(verifier_connexion)):
    """Met à jour le score d'un genre pour la personne connectée.
    aime=true → +1 point, aime=false → -0.2 point (jamais sous 0)."""
    variation = 1.0 if aime else -0.2
    maintenant = datetime.now(timezone.utc)

    with get_connexion() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO preferences (utilisateur_id, genre, score, maj_le)
                VALUES (%s, %s, GREATEST(0, %s), %s)
                ON CONFLICT (utilisateur_id, genre)
                DO UPDATE SET score = GREATEST(0, preferences.score + %s), maj_le = %s
            """, (utilisateur_id, genre, variation, maintenant, variation, maintenant))
        conn.commit()

    return {"statut": "ok", "genre": genre}
