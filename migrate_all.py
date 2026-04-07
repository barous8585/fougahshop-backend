"""
Migration complète — FougahShop
Ajoute toutes les colonnes manquantes en une seule passe.
À exécuter UNE SEULE FOIS dans le Shell Render.

Usage :
    python3 migrate_all.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from database import engine
from sqlalchemy import text

MIGRATIONS = [
    # table          colonne              définition SQL
    ("config",    "tarifs_unite",    "TEXT DEFAULT NULL"),
    ("config",    "tarif_poids_kg",  "FLOAT DEFAULT 12.0"),
    ("employes",  "role",            "VARCHAR DEFAULT 'employe'"),
    ("port_kg",   "actif",           "BOOLEAN DEFAULT TRUE"),
]

def run():
    ok, skip, err = 0, 0, 0
    with engine.connect() as conn:
        for table, col, definition in MIGRATIONS:
            sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition}"
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"✅  {table}.{col} ajouté")
                ok += 1
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    print(f"ℹ️   {table}.{col} existe déjà — ignoré")
                    skip += 1
                else:
                    print(f"❌  {table}.{col} — erreur : {e}")
                    err += 1

        # Mettre à jour les employes existants sans rôle
        try:
            conn.execute(text(
                "UPDATE employes SET role = 'employe' WHERE role IS NULL OR role = ''"
            ))
            conn.commit()
            print("✅  Rôles employés existants initialisés")
        except Exception as e:
            print(f"⚠️   Mise à jour rôles : {e}")

    print(f"\n{'='*40}")
    print(f"Migration terminée — {ok} ajout(s), {skip} ignoré(s), {err} erreur(s)")
    if err:
        print("⚠️  Des erreurs se sont produites. Vérifiez les logs ci-dessus.")
        sys.exit(1)
    else:
        print("✅  Redémarrez le serveur Render pour appliquer les changements.")

if __name__ == "__main__":
    run()
