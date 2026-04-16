from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
from database import engine, Base
import models  # noqa
from routes.commandes import router as commandes_router
from routes.admin     import router as admin_router
from routes.auth      import router as auth_router
from routes.config    import router as config_router
from routes.promo     import router as promos_router   # fichier routes/promo.py existant
from routes.notifs    import router as notifs_router
from routes.avis      import router as avis_router
from routes.scraper   import router as scraper_router
from routes.whatsapp  import router as whatsapp_router
from routes.config    import init_port, get_config
from database import SessionLocal
from parrainage import router as parrainage_router

# ── Créer les tables ──────────────────────────────────────────
Base.metadata.create_all(bind=engine)

# ── Initialiser les données par défaut ────────────────────────
def startup():
    db = SessionLocal()
    try:
        cfg = get_config(db)
        init_port(db)

        if not cfg.admin_pwd or cfg.admin_pwd == '':
            cfg.admin_pwd = 'admin123'
            db.commit()
            print("✅ Mot de passe admin initialisé (valeur par défaut)")
        else:
            print("✅ Mot de passe admin configuré")

        # ✅ Secret reset lu depuis variable d'environnement — jamais en dur dans le code
        secret_env = os.environ.get("SECRET_RESET", "")
        if secret_env and (not cfg.secret_reset or cfg.secret_reset == ''):
            cfg.secret_reset = secret_env
            db.commit()
            print("✅ Secret reset initialisé depuis variable d'environnement")
        elif cfg.secret_reset:
            print("✅ Secret reset configuré")
        else:
            print("⚠️  SECRET_RESET non défini — définissez la variable d'environnement")

    finally:
        db.close()

startup()

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="FougahShop API", version="2.1.0")

# ── CORS ─────────────────────────────────────────────────────
ALLOWED_ORIGINS = [
    "https://fougahshop.com",
    "https://www.fougahshop.com",
    "http://localhost:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
    allow_credentials=False,
)

# ── Routes API ────────────────────────────────────────────────
app.include_router(commandes_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(config_router)
app.include_router(promos_router)        # ✅ nouveau router promos
app.include_router(notifs_router)
app.include_router(avis_router)
app.include_router(scraper_router)
app.include_router(whatsapp_router)
app.include_router(parrainage_router)

# ── Frontend statique ─────────────────────────────────────────
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=FileResponse)
    def root():
        return os.path.join(static_dir, "index.html")

# ── Health check ──────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "2.1.0"}

# ── Documentation des routes ──────────────────────────────────
@app.get("/api")
def api_info():
    return {
        "app": "FougahShop API",
        "version": "2.1.0",
        "endpoints": [
            # Commandes (public)
            "GET  /api/config/public",
            "POST /api/commandes/",
            "GET  /api/commandes/suivi/{ref}",
            "GET  /api/commandes/historique/{tel}",
            "POST /api/commandes/annuler",
            # Auth
            "POST /api/auth/login",
            "POST /api/auth/reset",
            "GET  /api/auth/check",
            # Admin commandes
            "GET  /api/admin/stats",
            "GET  /api/admin/commandes",
            "PATCH /api/admin/commandes/{ref}/statut",
            "GET  /api/admin/export/csv",
            "POST /api/admin/commandes/{ref}/archiver",
            "POST /api/admin/commandes/{ref}/desarchiver",
            "GET  /api/admin/commandes/archives",
            # Codes promo (nouveau — préfixe /api/promos)
            "GET  /api/promos/verifier/{code}",   # public — client
            "POST /api/promos/verifier",          # rétro-compat
            "GET  /api/promos/admin",             # admin
            "POST /api/promos",                   # admin — créer
            "PATCH /api/promos/{code}/toggle",    # admin — activer/désactiver
            "PATCH /api/promos/admin/{id}",       # admin — modifier par ID
            "DELETE /api/promos/{code}",          # admin — supprimer par code
            "DELETE /api/promos/admin/{id}",      # admin — supprimer par ID
            # Notifs
            "POST /api/notifs/register",
            "POST /api/notifs/send",
            # Avis
            "GET  /api/avis/",
            "POST /api/avis/",
            "GET  /api/avis/admin",
            "PATCH /api/avis/admin/{id}/reponse",
            "PATCH /api/avis/admin/{id}/visibilite",
            "DELETE /api/avis/admin/{id}",
            # Config
            "GET  /api/config/pays",
            "PATCH /api/config/pays/{pays}/toggle",
            "POST /api/config/employes",
            "DELETE /api/config/employes/{id}",
            "GET  /api/config/employes",
            "PUT  /api/config/port",
            "PUT  /api/config/",
            # Scraper
            "POST /api/scraper/produit",
            "POST /api/scraper/panier",
            # Parrainage
            "GET  /api/parrainage/code/{tel}",
            "GET  /api/parrainage/verifier/{code}",
            "POST /api/parrainage/utiliser",
            "GET  /api/admin/parrainage",
            # Galerie
            "GET  /api/galerie",
            "GET  /api/admin/galerie-all",
            "POST /api/admin/galerie",
            "DELETE /api/admin/galerie/{id}",
            "PATCH /api/admin/galerie/{id}/toggle",
        ]
    }
