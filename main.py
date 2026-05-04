from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os, asyncio
from security import SecurityMiddleware, cleanup_rate_limits
from database import engine, Base, SessionLocal
import models  # noqa — déclenche la création des tables

# ── Imports routers ───────────────────────────────────────────
from routes.commandes  import router as commandes_router
from routes.admin      import router as admin_router
from routes.auth       import router as auth_router
from routes.config     import router as config_router
from routes.promo      import router as promos_router
from routes.notifs     import router as notifs_router
from routes.avis       import router as avis_router
from routes.whatsapp   import router as whatsapp_router
from routes.parrainage import router as parrainage_router
from routes.annonce    import router as annonce_router

# ── Imports fonctions startup ─────────────────────────────────
from routes.promo      import ensure_tables as ensure_promo_tables
from routes.annonce    import ensure_annonces_table
from routes.admin      import ensure_archived_column
from routes.auth       import ensure_sessions_table, purge_expired_sessions
from routes.notifs     import ensure_tokens_table, purge_old_tokens
from routes.parrainage import ensure_parrainage_tables
from routes.config     import (
    init_port, get_config, ensure_tarifs_columns,
    auto_refresh_taux_gnf, refresh_taux_gnf_en_base
)

# ── Créer les tables SQLAlchemy ───────────────────────────────
Base.metadata.create_all(bind=engine)


# ══════════════════════════════════════════════════════════════
# LIFESPAN — remplace @app.on_event("startup") déprécié
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    ✅ Toutes les migrations et initialisations au démarrage.
    Utilise le lifespan context manager (FastAPI 0.95+).
    """
    db = SessionLocal()
    try:
        # ── Config de base ────────────────────────────────────
        cfg = get_config(db)
        init_port(db)

        if not cfg.admin_pwd:
            cfg.admin_pwd = "admin123"
            db.commit()
            print("✅ Mot de passe admin initialisé (valeur par défaut)")
        else:
            print("✅ Mot de passe admin configuré")

        secret_env = os.environ.get("SECRET_RESET", "")
        if secret_env and not cfg.secret_reset:
            cfg.secret_reset = secret_env
            db.commit()
            print("✅ Secret reset initialisé depuis variable d'environnement")
        elif cfg.secret_reset:
            print("✅ Secret reset configuré")
        else:
            print("⚠️  SECRET_RESET non défini — définissez la variable d'environnement sur Render")

        # ── Migrations tables ─────────────────────────────────
        ensure_parrainage_tables(db)
        print("✅ Tables parrainage et galerie vérifiées")

        ensure_promo_tables(db)
        print("✅ Table promo_codes vérifiée")

        ensure_annonces_table(db)
        print("✅ Table annonces vérifiée")

        ensure_archived_column(db)
        print("✅ Colonne archived vérifiée")

        ensure_sessions_table(db)
        print("✅ Table admin_sessions vérifiée")

        ensure_tokens_table(db)
        print("✅ Table FCM tokens vérifiée")

        ensure_tarifs_columns(db)
        print("✅ Colonnes config vérifiées")

        # ── Table sessions WhatsApp ───────────────────────────
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

        # ── Nettoyages ────────────────────────────────────────
        purge_expired_sessions(db)
        print("✅ Sessions expirées nettoyées")

        purge_old_tokens(db)
        print("✅ Tokens FCM obsolètes nettoyés")

        # ── Taux GNF — première mise à jour ──────────────────
        try:
            await refresh_taux_gnf_en_base(db)
            print("✅ Taux GNF initialisé depuis open.er-api.com")
        except Exception as e:
            print(f"⚠️  Taux GNF non initialisé au startup: {e}")

    except Exception as e:
        print(f"❌ Erreur startup: {e}")
    finally:
        db.close()

    # ── Tâche de fond — refresh taux GNF toutes les heures ───
    task = asyncio.create_task(auto_refresh_taux_gnf())
    print("✅ Tâche auto-refresh taux GNF démarrée (toutes les heures)")

    task_cleanup = asyncio.create_task(cleanup_rate_limits())
    print("✅ Tâche nettoyage rate limits démarrée")

    yield  # ← l'app tourne ici

    # ── Shutdown ──────────────────────────────────────────────
    task_cleanup.cancel()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ══════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════

app = FastAPI(
    title    = "FougahShop API",
    version  = "2.3.0",
    lifespan = lifespan,
)

# ── CORS ──────────────────────────────────────────────────────
# En production, seul fougahshop.com est autorisé
# En développement, les localhost sont ajoutés
_is_prod = os.environ.get("RENDER", "") == "true"

ALLOWED_ORIGINS = [
    "https://fougahshop.com",
    "https://www.fougahshop.com",
] + ([] if _is_prod else [
    "http://localhost:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
])

# ── Sécurité — Rate limiting + Headers ───────────────────────
app.add_middleware(SecurityMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_methods     = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers     = ["Content-Type", "X-Admin-Token"],
    # ✅ allow_credentials=True pour que les cookies de session fonctionnent
    allow_credentials = True,
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
    return {"status": "ok", "version": "2.3.0"}

@app.get("/api")
def api_info():
    return {"app": "FougahShop API", "version": "2.3.0"}
