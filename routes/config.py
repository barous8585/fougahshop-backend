# ── PATCH pour routes/config.py ──────────────────────────────
# Ajouter require_patron sur les 3 routes non protégées
# Remplace uniquement les signatures de ces 3 fonctions

# ── AVANT (dans config.py) ────────────────────────────────────
# @router.put("/")
# def update_config(body: Dict[str, Any], db: Session = Depends(get_db)):

# @router.put("/port")
# def update_port(body: Dict[str, Any], db: Session = Depends(get_db)):

# @router.delete("/employes/{emp_id}")
# def delete_employe(emp_id: int, db: Session = Depends(get_db)):

# ── APRÈS ─────────────────────────────────────────────────────
# Ajouter "request: Request" et "role: str = Depends(require_patron)" à chaque route

@router.put("/")
def update_config(body: Dict[str, Any], request: Request,
                  db: Session = Depends(get_db),
                  role: str = Depends(require_patron)):   # ✅ PROTÉGÉ
    # ... (reste du code identique)

@router.put("/port")
def update_port(body: Dict[str, Any], request: Request,
                db: Session = Depends(get_db),
                role: str = Depends(require_patron)):     # ✅ PROTÉGÉ
    # ... (reste du code identique)

@router.delete("/employes/{emp_id}")
def delete_employe(emp_id: int, request: Request,
                   db: Session = Depends(get_db),
                   role: str = Depends(require_patron)):  # ✅ PROTÉGÉ
    # ... (reste du code identique)

# ── IMPORTANT : ajouter cet import en haut de config.py ──────
# from routes.auth import require_patron   ← déjà présent ✅
# from fastapi import Request              ← à ajouter si pas encore là
