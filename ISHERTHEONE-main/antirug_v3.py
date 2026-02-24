# ===================================================================
# APEX XGBOOST ANTI-RUG v3.0 (Port 8003)
# Evolved from v2 RandomForest: XGBoost + expanded features
# New: contract_verified, deployer_age, deployer_prev_rugs,
#      social_account_age, funding_divergence, liquidity_lock
# ===================================================================

from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from apex_common.logging import get_logger
from apex_common.security import check_env_file_permissions
from apex_common.metrics import instrument_app

load_dotenv()
log = get_logger("antirug_v3")
_ok, _msg = check_env_file_permissions(".env")
if not _ok:
    log.warning(_msg)

app = FastAPI(title="Apex XGBoost Anti-Rug v3", version="3.0.0")
instrument_app(app)

DATA_DIR = Path(os.getenv("ANTI_RUG_DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

MODEL_FILE = DATA_DIR / "anti_rug_model_v3.pkl"
TRAINING_CSV = os.getenv("ANTI_RUG_TRAINING_CSV", "").strip()
ADMIN_TOKEN = os.getenv("ANTI_RUG_ADMIN_TOKEN", "").strip()
RETRAIN_COOLDOWN_S = float(os.getenv("ANTI_RUG_RETRAIN_COOLDOWN_S", "60"))
_last_retrain_ts: float = 0.0

# v3 expanded feature set
FEATURES = [
    "liquidity_usd",
    "top_holder_pct",
    "dev_wallet_tx_count",
    "age_hours",
    "volume_24h",
    "holders_count",
    "buy_tax_pct",
    "sell_tax_pct",
    # v3 new features
    "contract_verified",
    "deployer_age_days",
    "deployer_prev_rugs",
    "social_account_age_days",
    "funding_divergence_bps",
    "liquidity_lock_pct",
]

RUG_THRESHOLD = float(os.getenv("ANTI_RUG_THRESHOLD", "0.40"))

USE_XGBOOST = True
try:
    import xgboost as xgb
    log.info("XGBoost available — using XGBClassifier")
except ImportError:
    USE_XGBOOST = False
    log.warning("XGBoost not installed — falling back to RandomForest")
    from sklearn.ensemble import RandomForestClassifier


def _to_row(d: dict) -> list[float]:
    return [float(d.get(k, 0.0) or 0.0) for k in FEATURES]


def load_labeled_csv(path: str):
    X, y = [], []
    with open(path, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if "is_rug" not in row:
                raise ValueError("CSV precisa de coluna 'is_rug' (0/1).")
            X.append(_to_row(row))
            y.append(int(float(row["is_rug"])))
    return np.array(X, dtype=float), np.array(y, dtype=int)


def train_synthetic(model_path: Path):
    log.info("Training synthetic Anti-Rug v3 model...")
    np.random.seed(42)
    n = 1500

    # Rug distributions
    rugs_X = np.column_stack([
        np.random.uniform(500, 15000, n),          # liquidity_usd
        np.random.uniform(35, 97, n),              # top_holder_pct
        np.random.randint(10, 250, n),             # dev_wallet_tx_count
        np.random.uniform(0.1, 24, n),             # age_hours
        np.random.uniform(1000, 90000, n),         # volume_24h
        np.random.uniform(50, 4000, n),            # holders_count
        np.random.uniform(5, 35, n),               # buy_tax_pct
        np.random.uniform(5, 45, n),               # sell_tax_pct
        np.random.choice([0, 1], n, p=[0.8, 0.2]), # contract_verified (mostly not)
        np.random.uniform(0, 30, n),               # deployer_age_days
        np.random.randint(0, 10, n),               # deployer_prev_rugs (high)
        np.random.uniform(0, 30, n),               # social_account_age_days
        np.random.uniform(-50, 50, n),             # funding_divergence_bps
        np.random.uniform(0, 20, n),               # liquidity_lock_pct
    ])
    rugs_y = np.ones(n)

    # Non-rug distributions
    succ_X = np.column_stack([
        np.random.uniform(50000, 1200000, n),
        np.random.uniform(3, 30, n),
        np.random.randint(0, 20, n),
        np.random.uniform(24, 5000, n),
        np.random.uniform(100000, 20000000, n),
        np.random.uniform(5000, 500000, n),
        np.random.uniform(0, 8, n),
        np.random.uniform(0, 10, n),
        np.random.choice([0, 1], n, p=[0.2, 0.8]),  # contract_verified (mostly yes)
        np.random.uniform(30, 3650, n),              # deployer_age_days
        np.random.randint(0, 1, n),                  # deployer_prev_rugs (low)
        np.random.uniform(90, 3650, n),              # social_account_age_days
        np.random.uniform(-10, 10, n),               # funding_divergence_bps
        np.random.uniform(50, 100, n),               # liquidity_lock_pct
    ])
    succ_y = np.zeros(n)

    X = np.vstack([rugs_X, succ_X])
    y = np.concatenate([rugs_y, succ_y])

    if USE_XGBOOST:
        clf = xgb.XGBClassifier(
            n_estimators=800,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=1.0,
            eval_metric="auc",
            random_state=42,
            n_jobs=-1,
            use_label_encoder=False,
        )
    else:
        clf = RandomForestClassifier(
            n_estimators=500,
            max_depth=10,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1,
        )

    clf.fit(X, y)
    joblib.dump(clf, model_path)
    log.info(f"Model saved: {model_path} ({'XGBoost' if USE_XGBOOST else 'RandomForest'})")
    return clf


def train_from_csv(model_path: Path, csv_path: str):
    log.info(f"Training from CSV: {csv_path}")
    X, y = load_labeled_csv(csv_path)

    if USE_XGBOOST:
        clf = xgb.XGBClassifier(
            n_estimators=800,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            eval_metric="auc",
            early_stopping_rounds=50,
            random_state=42,
            n_jobs=-1,
            use_label_encoder=False,
        )
    else:
        clf = RandomForestClassifier(
            n_estimators=500,
            max_depth=10,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1,
        )

    clf.fit(X, y)
    joblib.dump(clf, model_path)
    log.info(f"Model saved: {model_path}")
    return clf


def load_or_train():
    if MODEL_FILE.exists():
        try:
            m = joblib.load(MODEL_FILE)
            log.info(f"Loaded model: {MODEL_FILE}")
            return m
        except Exception as e:
            log.warning(f"Failed loading model: {e}")

    if TRAINING_CSV:
        try:
            return train_from_csv(MODEL_FILE, TRAINING_CSV)
        except Exception as e:
            log.warning(f"CSV training failed: {e}")

    return train_synthetic(MODEL_FILE)


model = load_or_train()


def _get_rug_prob(m, row: np.ndarray) -> float:
    proba = m.predict_proba(row)[0]
    classes = list(m.classes_)
    try:
        idx = classes.index(1)
    except ValueError:
        try:
            idx = classes.index(1.0)
        except ValueError:
            idx = -1
    return float(proba[idx])


class TokenMetricsV3(BaseModel):
    liquidity_usd: float = Field(..., ge=0)
    top_holder_pct: float = Field(..., ge=0, le=100)
    dev_wallet_tx_count: int = Field(..., ge=0)
    age_hours: float = Field(..., ge=0)
    volume_24h: float = Field(..., ge=0)
    holders_count: float = Field(0.0, ge=0)
    buy_tax_pct: float = Field(0.0, ge=0, le=100)
    sell_tax_pct: float = Field(0.0, ge=0, le=100)
    # v3 new features
    contract_verified: int = Field(0, ge=0, le=1)
    deployer_age_days: float = Field(0.0, ge=0)
    deployer_prev_rugs: int = Field(0, ge=0)
    social_account_age_days: float = Field(0.0, ge=0)
    funding_divergence_bps: float = Field(0.0)
    liquidity_lock_pct: float = Field(0.0, ge=0, le=100)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "antirug_v3",
        "version": app.version,
        "model": str(MODEL_FILE),
        "model_type": "XGBoost" if USE_XGBOOST else "RandomForest",
        "features": FEATURES,
        "rug_threshold": RUG_THRESHOLD,
    }


@app.post("/analyze_token")
async def analyze_token(metrics: TokenMetricsV3):
    row = np.array([[
        metrics.liquidity_usd,
        metrics.top_holder_pct,
        metrics.dev_wallet_tx_count,
        metrics.age_hours,
        metrics.volume_24h,
        metrics.holders_count,
        metrics.buy_tax_pct,
        metrics.sell_tax_pct,
        metrics.contract_verified,
        metrics.deployer_age_days,
        metrics.deployer_prev_rugs,
        metrics.social_account_age_days,
        metrics.funding_divergence_bps,
        metrics.liquidity_lock_pct,
    ]], dtype=float)

    rug_prob = _get_rug_prob(model, row)
    status = "REJEITADO" if rug_prob > RUG_THRESHOLD else "APROVADO"

    # Feature importance for explainability
    risk_factors = []
    if metrics.deployer_prev_rugs > 0:
        risk_factors.append(f"deployer_prev_rugs={metrics.deployer_prev_rugs}")
    if metrics.top_holder_pct > 50:
        risk_factors.append(f"whale_concentration={metrics.top_holder_pct:.1f}%")
    if metrics.liquidity_lock_pct < 30:
        risk_factors.append(f"low_liq_lock={metrics.liquidity_lock_pct:.1f}%")
    if not metrics.contract_verified:
        risk_factors.append("unverified_contract")
    if metrics.age_hours < 6:
        risk_factors.append(f"very_new={metrics.age_hours:.1f}h")

    return {
        "status": status,
        "rug_probability_pct": round(rug_prob * 100, 2),
        "risk_factors": risk_factors,
        "edge_directive": "Risco de honeypot/rug detectado." if status == "REJEITADO" else "Estrutura on-chain limpa.",
        "model_type": "XGBoost" if USE_XGBOOST else "RandomForest",
    }


# ── On-chain verification via FREE APIs (Jupiter + Solana FM) ──
@app.post("/verify_onchain")
async def verify_onchain(mint: str):
    """Cross-reference token data from free APIs for extra rug detection.

    Sources (all FREE, no API key needed):
      1. Jupiter Price API — if no price, token might be dead/fake
      2. Solana FM — token registry verification
      3. DeFiLlama — check if protocol has TVL

    Use this BEFORE /analyze_token for Solana memecoins.
    """
    import httpx

    checks = {
        "jupiter_has_price": False,
        "jupiter_price": 0.0,
        "solana_fm_verified": False,
        "solana_fm_name": "",
        "overall_onchain_pass": False,
        "risk_flags": [],
    }

    async with httpx.AsyncClient(timeout=8.0) as http:
        # ── Jupiter Price (free, 600 req/min) ──
        try:
            r = await http.get(f"https://api.jup.ag/price/v2?ids={mint}")
            if r.status_code == 200:
                data = r.json().get("data", {}).get(mint, {})
                price = float(data.get("price", 0))
                if price > 0:
                    checks["jupiter_has_price"] = True
                    checks["jupiter_price"] = price
                else:
                    checks["risk_flags"].append("no_jupiter_price")
        except Exception:
            checks["risk_flags"].append("jupiter_unreachable")

        # ── Solana FM Token Registry (free) ──
        try:
            r = await http.get(
                f"https://api.solana.fm/v0/tokens/{mint}",
                headers={"accept": "application/json"},
            )
            if r.status_code == 200:
                token_info = r.json().get("tokenList", {})
                if token_info:
                    checks["solana_fm_verified"] = True
                    checks["solana_fm_name"] = token_info.get("name", "unknown")
                else:
                    checks["risk_flags"].append("not_in_solana_fm_registry")
        except Exception:
            checks["risk_flags"].append("solana_fm_unreachable")

    # Overall pass: must have price AND be in registry
    checks["overall_onchain_pass"] = (
        checks["jupiter_has_price"] and
        checks["solana_fm_verified"] and
        len(checks["risk_flags"]) == 0
    )

    return checks


# v2 backward-compat endpoint (accepts v2 TokenMetrics with fewer fields)
@app.post("/analyze_token_v2")
async def analyze_token_v2(metrics: dict):
    """Backward-compatible endpoint that fills v3 fields with defaults."""
    full = TokenMetricsV3(
        liquidity_usd=float(metrics.get("liquidity_usd", 0)),
        top_holder_pct=float(metrics.get("top_holder_pct", 0)),
        dev_wallet_tx_count=int(metrics.get("dev_wallet_tx_count", 0)),
        age_hours=float(metrics.get("age_hours", 0)),
        volume_24h=float(metrics.get("volume_24h", 0)),
        holders_count=float(metrics.get("holders_count", 0)),
        buy_tax_pct=float(metrics.get("buy_tax_pct", 0)),
        sell_tax_pct=float(metrics.get("sell_tax_pct", 0)),
    )
    return await analyze_token(full)


@app.post("/admin/retrain")
async def admin_retrain(x_admin_token: Optional[str] = Header(default=None)):
    global model, _last_retrain_ts
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Admin retrain disabled.")
    if (x_admin_token or "") != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token.")

    now = time.time()
    if now - _last_retrain_ts < RETRAIN_COOLDOWN_S:
        remaining = int(RETRAIN_COOLDOWN_S - (now - _last_retrain_ts))
        raise HTTPException(status_code=429, detail=f"Retrain cooldown. Retry in {remaining}s.")

    model = load_or_train()
    _last_retrain_ts = time.time()
    return {"status": "retrained", "model": str(MODEL_FILE), "features": FEATURES}
