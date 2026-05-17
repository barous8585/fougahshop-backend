"""
sync_historique.py — Script de synchronisation initiale
========================================================
Lance ce script UNE SEULE FOIS depuis le shell Render pour
importer toutes les commandes existantes dans l'Excel OneDrive.

Depuis le shell Render :
  python sync_historique.py
"""

import asyncio
import json
import sys
import os

# ── Charger les variables d'environnement ─────────────────────
# (déjà disponibles sur Render via Environment Variables)

async def main():
    print("🔄 Démarrage synchronisation historique...")

    # Connexion base de données
    try:
        from database import SessionLocal
        from models import Commande, Config
        from sqlalchemy import text
    except Exception as e:
        print(f"❌ Erreur import base de données: {e}")
        sys.exit(1)

    # Import OneDrive
    try:
        from routes.onedrive import (
            ajouter_commande_excel,
            get_access_token,
            download_excel,
            upload_excel
        )
    except Exception as e:
        print(f"❌ Erreur import OneDrive: {e}")
        sys.exit(1)

    # Test connexion OneDrive
    print("🔑 Test connexion Microsoft Graph...")
    token = await get_access_token()
    if not token:
        print("❌ Impossible d'obtenir un token. Vérifiez les variables MS_*")
        sys.exit(1)
    print("✅ Connexion Microsoft Graph OK")

    # Récupérer toutes les commandes
    db = SessionLocal()
    try:
        cfg = db.query(Config).first()
        taux_gnf = (cfg.taux_gnf if cfg else None) or 9500

        commandes = db.query(Commande)\
                      .order_by(Commande.created_at.asc())\
                      .all()

        total = len(commandes)
        print(f"📦 {total} commandes trouvées en base")

        if total == 0:
            print("ℹ️  Aucune commande à synchroniser")
            return

        # Synchroniser une par une
        succes = 0
        erreurs = 0

        for i, cmd in enumerate(commandes, 1):
            print(f"  [{i}/{total}] {cmd.ref} — {cmd.statut}...", end=" ")
            try:
                ok = await ajouter_commande_excel({
                    "ref":         cmd.ref,
                    "client_nom":  cmd.client_nom,
                    "client_tel":  cmd.client_tel,
                    "client_pays": cmd.client_pays,
                    "total_euro":  cmd.total_euro,
                    "monnaie":     cmd.monnaie,
                    "statut":      cmd.statut,
                    "articles":    cmd.articles,
                    "note_admin":  cmd.note_admin,
                    "promo_code":  cmd.promo_code,
                    "created_at":  cmd.created_at,
                    "taux_gnf":    taux_gnf,
                })
                if ok:
                    print("✅")
                    succes += 1
                else:
                    print("❌ Échec upload")
                    erreurs += 1
            except Exception as e:
                print(f"❌ Erreur: {e}")
                erreurs += 1

            # Pause courte pour ne pas surcharger l'API OneDrive
            await asyncio.sleep(1)

        print(f"\n🎉 Synchronisation terminée !")
        print(f"   ✅ {succes} commandes synchronisées")
        if erreurs:
            print(f"   ❌ {erreurs} erreurs")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
