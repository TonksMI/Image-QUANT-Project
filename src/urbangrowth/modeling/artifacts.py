"""Model artifact persistence with SHA-256 integrity checks.

Saves model weights, preprocessing objects (scalers, imputers), and feature name
lists as pickled files under <data_root>/artifacts/. Maintains a manifest.json
with SHA-256 hashes so any tampered or corrupted artifact is detected on load.

Usage
-----
    from urbangrowth.modeling.artifacts import save_artifact, load_artifact

    save_artifact(model, "ridge_h1", metadata={"model": "ridge", "horizon": 1})
    model = load_artifact("ridge_h1")
"""
from __future__ import annotations

import hashlib
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_artifact(
    obj: Any,
    name: str,
    metadata: dict | None = None,
    artifact_dir: Path | None = None,
) -> Path:
    """Pickle *obj* and record its SHA-256 hash in the manifest.

    Parameters
    ----------
    obj         : any pickle-serialisable object
    name        : artifact stem, e.g. "ridge_h1" — do NOT include .pkl
    metadata    : arbitrary key/value context stored in the manifest
    artifact_dir: override default data_path("artifacts")

    Returns
    -------
    Path to the written .pkl file.
    """
    if artifact_dir is None:
        from urbangrowth.config import data_path
        artifact_dir = data_path("artifacts")

    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    pkl_path = artifact_dir / f"{name}.pkl"
    payload  = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    pkl_path.write_bytes(payload)

    sha = _sha256_bytes(payload)
    log.info("artifact_saved", name=name, sha256_prefix=sha[:12], path=str(pkl_path))

    manifest_path = artifact_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    manifest[name] = {
        "file":     pkl_path.name,
        "sha256":   sha,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata or {},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return pkl_path


def load_artifact(name: str, artifact_dir: Path | None = None) -> Any:
    """Load a pickled artifact after verifying its SHA-256 hash.

    Raises
    ------
    FileNotFoundError if the manifest or pickle file is missing.
    KeyError          if *name* is not in the manifest.
    ValueError        if the hash does not match (file corrupted or tampered).
    """
    if artifact_dir is None:
        from urbangrowth.config import data_path
        artifact_dir = data_path("artifacts")

    artifact_dir  = Path(artifact_dir)
    manifest_path = artifact_dir / "manifest.json"

    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest at {manifest_path}")

    manifest = _load_manifest(manifest_path)
    if name not in manifest:
        raise KeyError(f"Artifact '{name}' not found in manifest")

    entry    = manifest[name]
    pkl_path = artifact_dir / entry["file"]

    if not pkl_path.exists():
        raise FileNotFoundError(f"Artifact file missing: {pkl_path}")

    payload  = pkl_path.read_bytes()
    actual   = _sha256_bytes(payload)
    expected = entry["sha256"]

    if actual != expected:
        raise ValueError(
            f"Artifact '{name}' hash mismatch — "
            f"expected {expected[:12]}…, got {actual[:12]}…"
        )

    log.info("artifact_loaded", name=name, sha256_prefix=actual[:12])
    return pickle.loads(payload)  # noqa: S301  (trusted internal artifact)


def list_artifacts(artifact_dir: Path | None = None) -> list[dict]:
    """Return all manifest entries as a list of dicts (one per artifact)."""
    if artifact_dir is None:
        from urbangrowth.config import data_path
        artifact_dir = data_path("artifacts")

    manifest_path = Path(artifact_dir) / "manifest.json"
    if not manifest_path.exists():
        return []

    manifest = _load_manifest(manifest_path)
    return [{"name": k, **v} for k, v in manifest.items()]


def delete_artifact(name: str, artifact_dir: Path | None = None) -> bool:
    """Remove an artifact file and its manifest entry. Returns True if deleted."""
    if artifact_dir is None:
        from urbangrowth.config import data_path
        artifact_dir = data_path("artifacts")

    artifact_dir  = Path(artifact_dir)
    manifest_path = artifact_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)

    if name not in manifest:
        return False

    pkl_path = artifact_dir / manifest[name]["file"]
    if pkl_path.exists():
        pkl_path.unlink()

    del manifest[name]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("artifact_deleted", name=name)
    return True
