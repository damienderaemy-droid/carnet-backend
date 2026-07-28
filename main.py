"""
Étape 1 — Backend minimal
==========================
Objectif : juste prouver que le "tuyau" fonctionne — un serveur en ligne
que ton appli pourra appeler. Pas encore de vraie logique métier ici.
"""

from fastapi import FastAPI

app = FastAPI(title="Carnet - API")


@app.get("/")
def accueil():
    """Vérifie que le serveur répond."""
    return {"statut": "ok", "message": "Le backend fonctionne !"}


@app.get("/activites")
def activites():
    """Étape 1 : données factices, juste pour tester la connexion.
    À l'étape 2, ceci sera remplacé par un vrai appel à l'API Switzerland Tourism."""
    return {
        "activites": [
            {"nom": "Lac souterrain de Saint-Léonard", "genre": "nature"},
            {"nom": "Château de Villa", "genre": "culture"},
            {"nom": "Via ferrata du Belvédère", "genre": "adventure"},
        ]
    }
