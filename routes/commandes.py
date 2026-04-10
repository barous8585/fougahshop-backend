# ── PATCH pour routes/commandes.py ───────────────────────────
# Remplace uniquement les deux fonctions suivi() et historique()
# Le reste du fichier reste identique

@router.get("/suivi/{ref}")
def suivi(ref: str, db: Session = Depends(get_db)):
    cmd = db.query(Commande).filter(Commande.ref == ref.upper()).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    return {
        "ref":            cmd.ref,
        "statut":         cmd.statut,
        "client_nom":     cmd.client_nom,                          # ✅ AJOUTÉ
        "client_tel":     cmd.client_tel,
        "nb_articles":    cmd.nb_articles,
        "total_local":    cmd.total_local,
        "monnaie":        cmd.monnaie,
        "poids_estime":   cmd.poids_estime,
        "poids_reel":     cmd.poids_reel,
        "delai_livraison": cmd.delai_livraison,
        "articles":       json.loads(cmd.articles) if cmd.articles else [],
        "note_admin":     cmd.note_admin,
        "suivi_num":      getattr(cmd, "suivi_num", None),         # ✅ AJOUTÉ
        "motif_refus":    getattr(cmd, "motif_refus", None),       # ✅ AJOUTÉ
        "created_at":     cmd.created_at,
    }

@router.get("/historique/{tel}")
def historique(tel: str, db: Session = Depends(get_db)):
    tel_clean = tel.replace(" ", "").replace("+", "")
    cmds = db.query(Commande).filter(
        Commande.client_tel.contains(tel_clean[-8:])
    ).order_by(Commande.created_at.desc()).all()
    if not cmds:
        raise HTTPException(404, "Aucune commande trouvée")
    return [
        {
            "ref":             c.ref,
            "statut":          c.statut,
            "nb_articles":     c.nb_articles,
            "total_local":     c.total_local,
            "monnaie":         c.monnaie,
            "delai_livraison": c.delai_livraison,
            "note_admin":      c.note_admin,
            "client_nom":      c.client_nom,
            "client_tel":      c.client_tel,    # ✅ AJOUTÉ
            "created_at":      c.created_at,
        }
        for c in cmds
    ]
