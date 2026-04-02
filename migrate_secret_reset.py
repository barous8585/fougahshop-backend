"""
Script de migration — Ajoute la colonne secret_reset à la table config
À exécuter UNE SEULE FOIS sur ton serveur avant de redémarrer l'app.

Usage :
    python migrate_secret_reset.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from database import engine

def migrate():
    with engine.connect() as conn:
        # Vérifier si la colonne existe déjà
        try:
            from sqlalchemy import text
            # Tenter d'ajouter la colonne
            conn.execute(text(
                "ALTER TABLE config ADD COLUMN secret_reset VARCHAR DEFAULT 'fougah2026'"
            ))
            conn.commit()
            print("✅ Colonne secret_reset ajoutée avec succès.")
            print("   Valeur par défaut : fougah2026")
            print("   → Tu peux la changer dans l'interface admin ou en base directement.")
        except Exception as e:
            err = str(e).lower()
            if "duplicate" in err or "already exists" in err:
                print("ℹ️  La colonne secret_reset existe déjà — rien à faire.")
            else:
                print(f"❌ Erreur inattendue : {e}")
                sys.exit(1)

if __name__ == "__main__":
    migrate()
