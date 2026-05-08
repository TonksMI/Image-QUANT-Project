"""Land cover segmentation of Sentinel-2 monthly composites.

Primary model  : DynamicWorld v1 architecture (Unet / efficientnet-b4)
Fallback model : Prithvi-100M backbone (ibm-nasa-geospatial/Prithvi-100M)
                 with a lightweight decoder head

Inference pipeline
  1. Load 6 spectral bands from the 9-band composite COG.
  2. Normalise to [0, 1] (divide by 10 000 DN).
  3. Tile with overlap; auto-tune batch size for the available VRAM.
  4. Run batched forward pass.
  5. Stitch probability maps with Gaussian blending in overlap zones.
  6. Argmax → uint8 class raster.
  7. Write class raster + float16 9-band probability raster.

Output paths per composite (derived from output_path argument):
  {output_path}                      – uint8 class raster (single band, 0–8)
  {output_path.stem}_prob.tif        – float16 probability raster (9 bands)

DynamicWorld classes (index → name):
  0 water  1 trees  2 grass  3 flooded_veg  4 crops
  5 shrub_scrub  6 built  7 bare  8 snow_ice

CLI: ug segment run --city phoenix --start 2018-01 --end 2025-12 --model dynamic_world
"""
from __future__ import annotations

import gc
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
import rioxarray
import structlog
import torch
import torch.nn as nn
import torch.nn.functional as F
from dotenv import load_dotenv
from rasterio.transform import Affine

from urbangrowth.config import data_path, get_cities, get_pipeline

load_dotenv()
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLASSES: list[str] = [
    "water", "trees", "grass", "flooded_veg", "crops",
    "shrub_scrub", "built", "bare", "snow_ice",
]
N_CLASSES = len(CLASSES)

# Composite band order (indices 0-5 are spectral; 6-8 are indices)
COMPOSITE_BANDS = ["B02", "B03", "B04", "B08", "B11", "B12", "NDVI", "NDBI", "NDWI"]
INPUT_BAND_IDX  = list(range(6))   # only spectral bands fed to model
N_INPUT_BANDS   = 6
DN_SCALE        = 10_000.0         # S2 L2A DN → [0, 1]

# colour palette for visualization (RGB tuples 0-255)
CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "water":       (0,   80, 160),
    "trees":       (20, 140,  30),
    "grass":       (120, 200,  60),
    "flooded_veg": (70, 180, 130),
    "crops":       (230, 200,  50),
    "shrub_scrub": (160, 130,  50),
    "built":       (200,  50,  50),
    "bare":        (190, 160, 110),
    "snow_ice":    (240, 240, 255),
}

_MODEL_DIR_KEY = "model_dir"


# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------


def _build_dynamic_world_model(device: str) -> nn.Module:
    """Unet with EfficientNet-B4 encoder — mirrors the DynamicWorld paper."""
    try:
        import segmentation_models_pytorch as smp
    except ImportError as exc:
        raise ImportError(
            "Install segmentation-models-pytorch: pip install segmentation-models-pytorch"
        ) from exc

    model = smp.Unet(
        encoder_name="efficientnet-b4",
        encoder_weights="imagenet",
        in_channels=N_INPUT_BANDS,
        classes=N_CLASSES,
        activation=None,
    )
    return model.to(device)


def _build_prithvi_model(device: str) -> nn.Module:
    """Prithvi-100M ViT backbone with lightweight convolutional segmentation head.

    Downloads backbone from HuggingFace if not cached locally.
    Falls back to ResNet-50 Unet if transformers / model weights unavailable.
    """
    try:
        from transformers import AutoConfig, AutoModel

        hf_repo = "ibm-nasa-geospatial/Prithvi-100M"
        log.info("prithvi_backbone_loading", repo=hf_repo)

        cfg = AutoConfig.from_pretrained(hf_repo, trust_remote_code=True)
        cfg.num_frames = 1
        cfg.in_chans   = N_INPUT_BANDS

        class PrithviSegHead(nn.Module):
            def __init__(self):
                super().__init__()
                self.backbone = AutoModel.from_pretrained(
                    hf_repo, config=cfg, trust_remote_code=True, ignore_mismatched_sizes=True
                )
                embed_dim = getattr(cfg, "embed_dim", 768)
                self.head = nn.Sequential(
                    nn.Conv2d(embed_dim, 256, 1),
                    nn.GELU(),
                    nn.Conv2d(256, N_CLASSES, 1),
                )

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                B, C, H, W = x.shape
                # Prithvi expects (B, T, C, H, W)
                x_t = x.unsqueeze(1)
                feats = self.backbone(x_t, return_dict=False)[0]  # (B, N_patches, embed_dim)
                # Reconstruct spatial grid — assume patch_size = 16
                patch_size = getattr(cfg, "patch_size", 16)
                gh = H // patch_size
                gw = W // patch_size
                feats = feats[:, 1:, :]           # drop cls token if present
                feats = feats[:, :gh * gw, :]
                feats = feats.permute(0, 2, 1).reshape(B, -1, gh, gw)
                feats = F.interpolate(feats, size=(H, W), mode="bilinear", align_corners=False)
                return self.head(feats)

        model = PrithviSegHead().to(device)
        log.info("prithvi_model_built", device=device)
        return model

    except Exception as exc:
        log.warning(
            "prithvi_load_failed_using_resnet50_fallback",
            error=str(exc),
        )
        # Fallback: ResNet-50 Unet (no imagenet weights — user must provide checkpoint)
        import segmentation_models_pytorch as smp

        model = smp.Unet(
            encoder_name="resnet50",
            encoder_weights=None,
            in_channels=N_INPUT_BANDS,
            classes=N_CLASSES,
            activation=None,
        )
        return model.to(device)


# ---------------------------------------------------------------------------
# Tiling helpers
# ---------------------------------------------------------------------------


def _gaussian_kernel(tile_size: int) -> np.ndarray:
    """2-D Gaussian weight map — peaks at center, tapers at edges."""
    sigma = tile_size / 4.0
    ax = np.arange(tile_size) - tile_size / 2.0
    xx, yy = np.meshgrid(ax, ax)
    kernel = np.exp(-(xx**2 + yy**2) / (2 * sigma**2)).astype(np.float32)
    return kernel


def _tile_positions(H: int, W: int, tile_size: int, overlap: int) -> list[tuple[int, int]]:
    """Return (y0, x0) top-left corners for all tiles covering the raster."""
    stride = tile_size - overlap
    positions: list[tuple[int, int]] = []
    y = 0
    while True:
        x = 0
        y0 = min(y, max(0, H - tile_size))
        while True:
            x0 = min(x, max(0, W - tile_size))
            positions.append((y0, x0))
            if x0 + tile_size >= W:
                break
            x += stride
        if y0 + tile_size >= H:
            break
        y += stride
    # Deduplicate while preserving order
    seen: set[tuple[int, int]] = set()
    unique: list[tuple[int, int]] = []
    for p in positions:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _extract_tile(arr: np.ndarray, y0: int, x0: int, tile_size: int) -> np.ndarray:
    """Extract and zero-pad a (C, tile_size, tile_size) tile from (C, H, W) arr."""
    H, W = arr.shape[1], arr.shape[2]
    tile = np.zeros((arr.shape[0], tile_size, tile_size), dtype=arr.dtype)
    y1 = min(y0 + tile_size, H)
    x1 = min(x0 + tile_size, W)
    tile[:, : y1 - y0, : x1 - x0] = arr[:, y0:y1, x0:x1]
    return tile


def _stitch_probs(
    prob_tiles: list[np.ndarray],
    positions: list[tuple[int, int]],
    H: int,
    W: int,
    tile_size: int,
) -> np.ndarray:
    """Gaussian-weighted stitching of probability tiles into a (N_CLASSES, H, W) map."""
    kernel = _gaussian_kernel(tile_size)
    acc  = np.zeros((N_CLASSES, H, W), dtype=np.float32)
    wsum = np.zeros((H, W),            dtype=np.float32)

    for probs, (y0, x0) in zip(prob_tiles, positions):
        y1 = min(y0 + tile_size, H)
        x1 = min(x0 + tile_size, W)
        th = y1 - y0
        tw = x1 - x0
        acc[:, y0:y1, x0:x1] += probs[:N_CLASSES, :th, :tw] * kernel[:th, :tw]
        wsum[y0:y1, x0:x1]   += kernel[:th, :tw]

    wsum = np.maximum(wsum, 1e-8)
    return acc / wsum


# ---------------------------------------------------------------------------
# LandCoverSegmenter
# ---------------------------------------------------------------------------


class LandCoverSegmenter:
    """Wraps a pretrained land cover model for tile-based raster inference.

    Parameters
    ----------
    model_name:
        ``"dynamic_world"`` (Unet/EfficientNet-B4) or ``"prithvi"`` (Prithvi-100M).
    device:
        PyTorch device string. Defaults to ``"cuda"`` if available, else ``"cpu"``.
    """

    def __init__(
        self,
        model_name: str = "dynamic_world",
        device: str = "cuda",
    ) -> None:
        if device == "cuda" and not torch.cuda.is_available():
            log.warning("cuda_not_available_falling_back_to_cpu")
            device = "cpu"
        self.model_name = model_name
        self.device     = device

        log.info("segmenter_init", model=model_name, device=device)
        self.model = self._build_model()
        self._load_checkpoint_if_exists()
        self.model.eval()

        # Batch size determined during first call to infer_raster
        self._batch_size: Optional[int] = None

    # ------------------------------------------------------------------
    # Model construction
    # ------------------------------------------------------------------

    def _build_model(self) -> nn.Module:
        if self.model_name == "dynamic_world":
            return _build_dynamic_world_model(self.device)
        elif self.model_name == "prithvi":
            return _build_prithvi_model(self.device)
        else:
            raise ValueError(
                f"Unknown model '{self.model_name}'. "
                "Options: 'dynamic_world', 'prithvi'."
            )

    def _checkpoint_path(self) -> Path:
        pipe = get_pipeline()
        model_dir = Path(pipe.get(_MODEL_DIR_KEY, "C:/urbangrowth_data/models"))
        return model_dir / "segmentation" / f"{self.model_name}.pth"

    def _load_checkpoint_if_exists(self) -> None:
        ckpt = self._checkpoint_path()
        if not ckpt.exists():
            log.info("no_checkpoint_found", path=str(ckpt), note="Using default weights.")
            return
        try:
            state = torch.load(ckpt, map_location=self.device)
            # Accept both raw state_dict and {'model': state_dict} wrappers
            if isinstance(state, dict) and "model" in state:
                state = state["model"]
            missing, unexpected = self.model.load_state_dict(state, strict=False)
            log.info(
                "checkpoint_loaded",
                path=str(ckpt),
                missing_keys=len(missing),
                unexpected_keys=len(unexpected),
            )
        except Exception as exc:
            log.warning("checkpoint_load_failed", path=str(ckpt), error=str(exc))

    def save_checkpoint(self) -> None:
        """Persist the current model weights to the standard checkpoint path."""
        ckpt = self._checkpoint_path()
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        torch.save({"model": self.model.state_dict()}, ckpt)
        log.info("checkpoint_saved", path=str(ckpt))

    # ------------------------------------------------------------------
    # VRAM auto-tune
    # ------------------------------------------------------------------

    def _tune_batch_size(self, tile_size: int, target_gb: float = 10.0) -> int:
        """Return largest batch_size whose forward pass fits in *target_gb* VRAM."""
        if self.device == "cpu":
            return 2

        candidates = [32, 16, 8, 4, 2, 1]
        for bs in candidates:
            try:
                torch.cuda.empty_cache()
                dummy = torch.randn(
                    bs, N_INPUT_BANDS, tile_size, tile_size,
                    device=self.device, dtype=torch.float32,
                )
                with torch.no_grad():
                    _ = self.model(dummy)
                del dummy
                torch.cuda.empty_cache()
                allocated_gb = torch.cuda.memory_allocated(self.device) / 1e9
                if allocated_gb < target_gb:
                    log.info("batch_size_tuned", batch_size=bs, vram_gb=round(allocated_gb, 2))
                    return bs
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                continue
            except Exception:
                break
        return 1

    # ------------------------------------------------------------------
    # Raster inference
    # ------------------------------------------------------------------

    def infer_raster(
        self,
        composite_path: Path,
        output_path: Path,
        tile_size: int = 512,
        overlap: int = 64,
    ) -> None:
        """Run land cover segmentation on a composite COG.

        Parameters
        ----------
        composite_path:
            Path to the 9-band monthly composite COG produced by composites.py.
        output_path:
            Path for the uint8 class raster output.
            The probability raster is written alongside as ``{stem}_prob.tif``.
        tile_size:
            Spatial tile size in pixels (default 512).
        overlap:
            Overlap in pixels between adjacent tiles (default 64).
        """
        output_path = Path(output_path)
        prob_path   = output_path.with_name(output_path.stem + "_prob.tif")

        if output_path.exists() and prob_path.exists():
            log.info("segmentation_exists", path=str(output_path))
            return

        t0 = time.perf_counter()

        # ── Load composite ────────────────────────────────────────────────────
        with rasterio.open(composite_path) as src:
            arr = src.read(np.array(INPUT_BAND_IDX) + 1).astype(np.float32)
            crs       = src.crs
            transform = src.transform
        _, H, W = arr.shape

        # Normalise to [0, 1]
        arr = np.clip(arr / DN_SCALE, 0.0, 1.0)

        # Replace NaN (masked pixels) with 0
        arr = np.nan_to_num(arr, nan=0.0)

        # ── Tile positions ────────────────────────────────────────────────────
        positions = _tile_positions(H, W, tile_size, overlap)
        log.info(
            "inference_start",
            composite=composite_path.name,
            shape=(H, W),
            n_tiles=len(positions),
        )

        # ── Auto-tune batch size on first call ────────────────────────────────
        if self._batch_size is None:
            self._batch_size = self._tune_batch_size(tile_size)

        # ── Batched inference ─────────────────────────────────────────────────
        prob_tiles: list[np.ndarray] = []
        batch_size = self._batch_size

        tile_buf: list[np.ndarray] = []
        pos_buf: list[tuple[int, int]] = []

        def _flush(tiles, positions_b):
            tensor = torch.tensor(np.stack(tiles), dtype=torch.float32, device=self.device)
            with torch.no_grad():
                logits = self.model(tensor)             # (B, 9, tile_size, tile_size)
                probs  = torch.softmax(logits, dim=1).cpu().numpy()
            for p in probs:
                prob_tiles.append(p)
            for pos in positions_b:
                pos_buf_out.append(pos)

        pos_buf_out: list[tuple[int, int]] = []

        for i, (y0, x0) in enumerate(positions):
            tile = _extract_tile(arr, y0, x0, tile_size)
            tile_buf.append(tile)
            pos_buf.append((y0, x0))

            if len(tile_buf) == batch_size or i == len(positions) - 1:
                _flush(tile_buf, pos_buf)
                tile_buf.clear()
                pos_buf.clear()

        # ── Stitch probabilities ──────────────────────────────────────────────
        full_probs = _stitch_probs(prob_tiles, pos_buf_out, H, W, tile_size)
        class_map  = full_probs.argmax(axis=0).astype(np.uint8)

        elapsed = time.perf_counter() - t0
        log.info("inference_done", composite=composite_path.name, elapsed_s=round(elapsed, 1))

        # ── Write class raster (uint8) ────────────────────────────────────────
        output_path.parent.mkdir(parents=True, exist_ok=True)
        profile_class = {
            "driver":    "GTiff",
            "dtype":     "uint8",
            "width":     W,
            "height":    H,
            "count":     1,
            "crs":       crs,
            "transform": transform,
            "compress":  "deflate",
            "tiled":     True,
            "blockxsize": 512,
            "blockysize": 512,
        }
        with rasterio.open(output_path, "w", **profile_class) as dst:
            dst.write(class_map[np.newaxis])
            dst.update_tags(classes=",".join(CLASSES))
            for i, name in enumerate(CLASSES):
                dst.update_tags(i + 1, class_name=name)
        log.info("class_raster_written", path=str(output_path))

        # ── Write probability raster (float16, 9 bands) ───────────────────────
        profile_prob = {**profile_class, "dtype": "float16", "count": N_CLASSES}
        with rasterio.open(prob_path, "w", **profile_prob) as dst:
            dst.write(full_probs.astype(np.float16))
            for i, name in enumerate(CLASSES):
                dst.update_tags(i + 1, class_name=name)
        log.info("prob_raster_written", path=str(prob_path))

        # Cleanup GPU memory
        if self.device == "cuda":
            gc.collect()
            torch.cuda.empty_cache()


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def _iter_months(start: str, end: str):
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


def run(
    city: str = "phoenix",
    start: str = "2018-01",
    end: str = "2025-12",
    model_name: str = "dynamic_world",
    device: str = "cuda",
) -> None:
    """Segment all available composites for *city* in [start, end].

    Resumable: months whose class raster already exists are skipped.
    """
    pipe = get_pipeline()
    composite_dir = data_path(pipe["processed_data_subdirs"]["composites"], city)
    out_dir       = data_path(pipe["processed_data_subdirs"]["land_cover"], city)

    segmenter = LandCoverSegmenter(model_name=model_name, device=device)

    months = list(_iter_months(start, end))
    log.info(
        "segmentation_run_start",
        city=city, start=start, end=end,
        n_months=len(months), model=model_name,
    )

    done = skipped = missing = 0
    t_total = time.perf_counter()

    for year, month in months:
        composite_path = composite_dir / f"{year:04d}-{month:02d}.tif"
        out_path       = out_dir       / f"{year:04d}-{month:02d}_class.tif"

        if not composite_path.exists():
            log.debug("composite_missing_skip", year=year, month=month)
            missing += 1
            continue

        if out_path.exists():
            skipped += 1
            continue

        segmenter.infer_raster(composite_path, out_path)
        done += 1

    elapsed = time.perf_counter() - t_total
    log.info(
        "segmentation_run_complete",
        city=city, total=len(months),
        done=done, skipped=skipped, no_composite=missing,
        elapsed_min=round(elapsed / 60, 1),
    )
