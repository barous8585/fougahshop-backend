from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os
from database import engine, Base, SessionLocal
import models  # noqa
from routes.commandes  import router as commandes_router
from routes.admin      import router as admin_router
from routes.auth       import router as auth_router
from routes.config     import router as config_router
from routes.promo      import router as promos_router
from routes.notifs     import router as notifs_router
from routes.avis       import router as avis_router
from routes.whatsapp   import router as whatsapp_router
from routes.parrainage import router as parrainage_router
from routes.promo      import ensure_tables as ensure_promo_tables
from routes.annonce    import router as annonce_router, ensure_annonces_table
from routes.admin      import ensure_archived_column
from routes.auth       import ensure_sessions_table, purge_expired_sessions
from routes.parrainage import ensure_parrainage_tables
from routes.config     import init_port, get_config, ensure_tarifs_columns, auto_refresh_taux_gnf, refresh_taux_gnf_en_base

# ── Créer les tables SQLAlchemy ───────────────────────────────
Base.metadata.create_all(bind=engine)

# ── Startup ───────────────────────────────────────────────────
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

        secret_env = os.environ.get("SECRET_RESET", "")
        if secret_env and (not cfg.secret_reset or cfg.secret_reset == ''):
            cfg.secret_reset = secret_env
            db.commit()
            print("✅ Secret reset initialisé depuis variable d'environnement")
        elif cfg.secret_reset:
            print("✅ Secret reset configuré")
        else:
            print("⚠️  SECRET_RESET non défini — définissez la variable d'environnement sur Render")

        # Tables parrainage + galerie (une seule fois)
        ensure_parrainage_tables(db)
        print("✅ Tables parrainage et galerie vérifiées")

        # ✅ Migration table promo_codes (une seule fois)
        ensure_promo_tables(db)
        print("✅ Table promo_codes vérifiée")

        # ✅ Migration table annonces
        ensure_annonces_table(db)
        print("✅ Table annonces vérifiée")

        # ✅ Migration colonne archived sur commandes
        ensure_archived_column(db)
        print("✅ Colonne archived vérifiée")

        # ✅ Migration table sessions admin
        ensure_sessions_table(db)
        print("✅ Table admin_sessions vérifiée")

        # ✅ Nettoyer les sessions expirées (> 7 jours)
        purge_expired_sessions(db)
        print("✅ Sessions expirées nettoyées")

        # ✅ Migrations colonnes config (une seule fois au startup)
        ensure_tarifs_columns(db)
        print("✅ Colonnes config vérifiées")

        # Table sessions WhatsApp
        from sqlalchemy import text
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS whatsapp_sessions (
                tel        VARCHAR PRIMARY KEY,
                data       TEXT NOT NULL DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        db.commit()
        print("✅ Table sessions WhatsApp vérifiée")

        # ✅ Première mise à jour du taux GNF au démarrage
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(refresh_taux_gnf_en_base(db))
            else:
                loop.run_until_complete(refresh_taux_gnf_en_base(db))
            print("✅ Taux GNF initialisé depuis open.er-api.com")
        except Exception as e:
            print(f"⚠️  Taux GNF non initialisé au startup: {e}")

    finally:
        db.close()

startup()

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="FougahShop API", version="2.2.0")

@app.on_event("startup")
async def on_startup():
    """Lance la mise à jour automatique du taux GNF toutes les heures."""
    import asyncio
    asyncio.create_task(auto_refresh_taux_gnf())
    print("✅ Tâche auto-refresh taux GNF démarrée (toutes les heures)")

# ── CORS ──────────────────────────────────────────────────────
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

# ── Routes ────────────────────────────────────────────────────
app.include_router(commandes_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(config_router)
app.include_router(promos_router)
app.include_router(notifs_router)
app.include_router(avis_router)
app.include_router(whatsapp_router)
app.include_router(parrainage_router)
app.include_router(annonce_router)
# scraper_router supprimé — scraper retiré du frontend

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
    return {"status": "ok", "version": "2.2.0"}

@app.get("/api")
def api_info():
    return {"app": "FougahShop API", "version": "2.2.0"}
