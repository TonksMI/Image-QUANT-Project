#!/usr/bin/env bash
# 00_setup_env.sh — Create conda environment and install PyTorch nightly
# Run from project root in Git Bash: bash scripts/00_setup_env.sh
# Requires: Git Bash, Miniconda/Anaconda, NVIDIA driver 570+ (CUDA 12.8)

set -euo pipefail

ENV_NAME="${1:-urbangrowth}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Resolve Windows PostgreSQL bin path in Git Bash
PG_BIN="/c/Program Files/PostgreSQL/16/bin"
export PATH="$PG_BIN:$PATH"

echo "========================================================"
echo " Urban Growth Research Platform — Environment Setup"
echo " Project root : $PROJECT_ROOT"
echo " Conda env    : $ENV_NAME"
echo "========================================================"

# ── 1. NVIDIA driver + CUDA check ─────────────────────────────────────────
echo ""
echo ">>> Step 1: Checking NVIDIA GPU and CUDA version..."
if ! command -v nvidia-smi &>/dev/null; then
    echo "ERROR: nvidia-smi not found. Install NVIDIA driver 570+ for CUDA 12.8 support."
    exit 1
fi

DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
CUDA_VER=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d ' ')
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)

echo "    GPU    : $GPU_NAME"
echo "    Driver : $DRIVER_VER"
echo "    CUDA CC: $CUDA_VER"

# Check CUDA 12.8+ (driver must report CUDA ≥ 12.8)
CUDA_RUNTIME=$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+' || echo "0.0")
echo "    Runtime CUDA: $CUDA_RUNTIME"
if python3 -c "v='$CUDA_RUNTIME'; parts=v.split('.'); exit(0 if int(parts[0])>12 or (int(parts[0])==12 and int(parts[1])>=8) else 1)" 2>/dev/null; then
    echo "    OK: CUDA $CUDA_RUNTIME >= 12.8"
else
    echo "    WARNING: CUDA $CUDA_RUNTIME detected — recommend >= 12.8 for Blackwell sm_120"
    echo "    Proceeding anyway (PyTorch nightly may still work)"
fi

# ── 2. Create or update conda environment ─────────────────────────────────
echo ""
echo ">>> Step 2: Creating conda environment '$ENV_NAME'..."
cd "$PROJECT_ROOT"

if conda env list | grep -q "^$ENV_NAME "; then
    echo "    Environment '$ENV_NAME' exists. Updating..."
    conda env update -n "$ENV_NAME" -f environment.yml --prune
else
    conda env create -n "$ENV_NAME" -f environment.yml
fi
echo "    OK: conda environment ready"

# ── 3. Install PyTorch nightly with CUDA 12.8 ─────────────────────────────
echo ""
echo ">>> Step 3: Installing PyTorch nightly (CUDA 12.8) for sm_120..."
conda run -n "$ENV_NAME" pip install --pre torch torchvision \
    --index-url https://download.pytorch.org/whl/nightly/cu128
echo "    OK: PyTorch nightly installed"

# ── 4. Verify torch.cuda.is_available() and compute capability ────────────
echo ""
echo ">>> Step 4: Verifying PyTorch CUDA + sm_120 compute capability..."
conda run -n "$ENV_NAME" python - <<'PYEOF'
import sys
import torch

assert torch.cuda.is_available(), \
    "FAIL: torch.cuda.is_available() returned False — check CUDA driver and PyTorch build"

cc = torch.cuda.get_device_capability()
name = torch.cuda.get_device_name(0)
cuda_ver = torch.version.cuda
torch_ver = torch.__version__

print(f"    Device     : {name}")
print(f"    Torch      : {torch_ver}")
print(f"    CUDA build : {cuda_ver}")
print(f"    Capability : {cc[0]}.{cc[1]} (sm_{cc[0]}{cc[1]:02d})")

if cc != (12, 0):
    print(f"    WARNING: Expected sm_120 (12,0), got {cc} — may be a different GPU or driver")
    print(f"             RTX 5070 (Blackwell) should report (12, 0)")
else:
    print(f"    OK: sm_120 Blackwell confirmed")
PYEOF

# ── 5. Install the package in editable mode ────────────────────────────────
echo ""
echo ">>> Step 5: Installing urbangrowth package (editable)..."
conda run -n "$ENV_NAME" pip install -e "$PROJECT_ROOT"
echo "    OK: 'ug' CLI available"

# ── 6. Create data directories ─────────────────────────────────────────────
echo ""
echo ">>> Step 6: Creating data directories at C:/urbangrowth_data/..."
DATA_DIRS=(
    "raw/census_bps" "raw/ferc_queue" "raw/usaspending" "raw/fred"
    "raw/sentinel2/phoenix" "raw/sentinel2/austin"
    "raw/elevation/phoenix" "raw/elevation/austin"
    "raw/osm/phoenix" "raw/osm/austin"
    "raw/city_permits/phoenix" "raw/city_permits/austin"
    "raw/parcels/phoenix" "raw/parcels/austin"
    "raw/zoning/phoenix" "raw/zoning/austin"
    "raw/markets" "raw/census_acs/phoenix" "raw/census_acs/austin"
    "raw/dot_tips/arizona" "raw/dot_tips/texas"
    "processed/composites/phoenix" "processed/composites/austin"
    "processed/land_cover/phoenix" "processed/land_cover/austin"
    "processed/change/phoenix" "processed/change/austin"
    "processed/h3_features" "processed/signals" "processed/features"
    "models"
)
for dir in "${DATA_DIRS[@]}"; do
    mkdir -p "/c/urbangrowth_data/$dir"
done
echo "    OK: data directories ready"

# ── 7. Copy .env if missing ────────────────────────────────────────────────
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    echo "    .env created — fill in CENSUS_API_KEY and FRED_API_KEY"
fi

echo ""
echo "========================================================"
echo " Setup complete."
echo " Next step: bash scripts/01_init_postgis.sh"
echo " Then run:  conda activate $ENV_NAME && ug doctor"
echo "========================================================"
