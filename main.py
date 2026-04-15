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
from routes.promo     import router as promo_router
from routes.notifs    import router as notifs_router
from routes.avis      import router as avis_router
from routes.scraper   import router as scraper_router
from routes.whatsapp  import router as whatsapp_router
from routes.config    import init_port, get_config
from database import SessionLocal
from parrainage import router as parrainage_router  # ✅ NOUVEAU

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

        if not cfg.secret_reset or cfg.secret_reset == '':
            cfg.secret_reset = 'fougah2026'
            db.commit()
            print("✅ Secret reset initialisé (valeur par défaut)")
        else:
            print("✅ Secret reset configuré")

    finally:
        db.close()

startup()

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="FougahShop API", version="2.0.0")

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
app.include_router(promo_router)
app.include_router(notifs_router)
app.include_router(avis_router)
app.include_router(scraper_router)
app.include_router(whatsapp_router)
app.include_router(parrainage_router)  # ✅ NOUVEAU

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
    return {"status": "ok", "version": "2.0.0"}

# ── Documentation des routes ──────────────────────────────────
@app.get("/api")
def api_info():
    return {
        "app": "FougahShop API",
        "version": "2.0.0",
        "endpoints": [
            "GET  /api/config/public",
            "POST /api/commandes/calculer",
            "POST /api/commandes/",
            "GET  /api/commandes/suivi/{ref}",
            "GET  /api/commandes/historique/{tel}",
            "POST /api/commandes/annuler",
            "POST /api/auth/login",
            "POST /api/auth/reset",
            "GET  /api/auth/check",
            "GET  /api/admin/stats",
            "GET  /api/admin/commandes",
            "PATCH /api/admin/commandes/{ref}/statut",
            "GET  /api/admin/export/csv",
            "POST /api/admin/commandes/{ref}/archiver",
            "POST /api/admin/commandes/{ref}/desarchiver",
            "GET  /api/admin/commandes/archives",
            "POST /api/promo/verifier",
            "GET  /api/promo/admin",
            "POST /api/promo/admin",
            "PATCH /api/promo/admin/{id}",
            "DELETE /api/promo/admin/{id}",
            "POST /api/notifs/register",
            "POST /api/notifs/send",
            "GET  /api/avis/",
            "POST /api/avis/",
            "GET  /api/avis/admin",
            "PATCH /api/avis/admin/{id}/reponse",
            "PATCH /api/avis/admin/{id}/visibilite",
            "DELETE /api/avis/admin/{id}",
            "GET  /api/config/pays",
            "PATCH /api/config/pays/{pays}/toggle",
            "POST /api/config/employes",
            "DELETE /api/config/employes/{id}",
            "GET  /api/config/employes",
            "PUT  /api/config/port",
            "PUT  /api/config/",
            "POST /api/scraper/produit",
            "POST /api/scraper/panier",
            "GET  /api/parrainage/code/{tel}",
            "GET  /api/parrainage/verifier/{code}",
            "POST /api/parrainage/utiliser",
            "GET  /api/admin/parrainage",
            "GET  /api/galerie",
            "GET  /api/admin/galerie-all",
            "POST /api/admin/galerie",
            "DELETE /api/admin/galerie/{id}",
            "PATCH /api/admin/galerie/{id}/toggle",
        ]
    }
