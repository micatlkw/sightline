from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from pathlib import Path
import yaml

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.auth.google_sso import User, get_current_user, is_admin_user, is_lan_client
from app.config import CameraConfig, Settings, normalize_class_confidence_thresholds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsUpdateRequest(BaseModel):
    yolo_model: Optional[str] = None
    yolo_model_sha256: Optional[str] = None
    target_classes: Optional[List[Any]] = None
    confidence_threshold: Optional[float] = Field(None, ge=0.01, le=100.0)
    class_confidence_thresholds: Optional[Dict[str, float]] = None
    sample_fps: Optional[float] = Field(None, ge=0.1, le=30.0)
    min_clip_keyframes: Optional[int] = Field(None, ge=0, le=100)
    vid_stride: Optional[int] = Field(None, ge=1)

    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def _validate_conf_threshold(cls, v: Any) -> float | None:
        if v is None or v == "":
            return None
        val = float(v)
        if val > 1.0:
            val = val / 100.0
        if not (0.01 <= val <= 1.0):
            raise ValueError("Confidence threshold must be between 0.01 and 1.0 (or 1% and 100%)")
        return round(val, 4)

    @field_validator("class_confidence_thresholds", mode="before")
    @classmethod
    def _validate_class_conf_thresholds(cls, v: Any) -> dict[str, float] | None:
        return normalize_class_confidence_thresholds(v)

    cameras: Optional[List[CameraConfig]] = None
    alert_cooldown_seconds: Optional[int] = Field(None, ge=0)
    apprise_urls: Optional[List[str]] = None
    clip_stable_seconds: Optional[float] = Field(None, ge=0.1)
    scan_on_startup: Optional[bool] = None
    watch_use_polling: Optional[bool] = None
    allowed_google_emails: Optional[List[str]] = None
    admin_google_emails: Optional[List[str]] = None
    google_client_id: Optional[str] = None
    allow_lan_auth_bypass: Optional[bool] = None
    domain_name: Optional[str] = None
    acme_email: Optional[str] = None
    https_port: Optional[int] = Field(None, ge=1, le=65535)
    http_port: Optional[int] = Field(None, ge=0, le=65535)
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    lan_hosts: Optional[List[str]] = None
    tunnel_mode: Optional[bool] = None


@router.get("", summary="Get current application settings")
async def get_settings(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    cfg: Settings = request.app.state.settings

    # WAN Security Policy Enforcement
    is_lan = is_lan_client(request, cfg)
    if not is_lan and not is_admin_user(current_user, cfg):
        logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} denied read access to settings over WAN")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to view settings over WAN",
        )

    return {
        "settings": cfg.export_editable_dict(),
        "config_file": str(cfg.settings_config_path),
        "is_lan": is_lan,
    }


@router.get("/models", summary="List locally cached YOLO models")
async def list_models(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    cfg: Settings = request.app.state.settings
    models_dir = Path(cfg.models_dir) if hasattr(cfg, "models_dir") else Path("/models")

    models = []
    if models_dir.is_dir():
        for ext in ("*.pt", "*.onnx", "*.engine"):
            for f in sorted(models_dir.glob(ext)):
                if f.is_file():
                    try:
                        size_mb = round(f.stat().st_size / (1024 * 1024), 1)
                    except Exception:
                        size_mb = 0.0
                    models.append({
                        "name": f.name,
                        "size_mb": size_mb,
                        "active": (f.name == cfg.yolo_model),
                    })

    # Sort so active model comes first, followed by name alphabetically
    models.sort(key=lambda m: (not m["active"], m["name"].lower()))
    return {
        "models": models,
        "current_model": cfg.yolo_model,
    }


def _settings_values_differ(old_val: Any, new_val: Any) -> bool:
    """Compares setting values accounting for None vs empty string/list equivalence."""
    if old_val == "" or old_val == [] or old_val is None:
        old_val = None
    if new_val == "" or new_val == [] or new_val is None:
        new_val = None
    if isinstance(old_val, str) and isinstance(new_val, str):
        return old_val.strip() != new_val.strip()
    return old_val != new_val


@router.put("", summary="Update application settings and persist to JSON config file")
async def update_settings(
    body: SettingsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    cfg: Settings = request.app.state.settings
    updates = body.model_dump(exclude_unset=True)

    # WAN vs LAN Security Policy Enforcement
    is_lan = is_lan_client(request, cfg)
    if not is_lan:
        # 1. Require admin privileges to modify any settings over WAN
        if not is_admin_user(current_user, cfg):
            logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} attempted to modify settings over WAN")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required to modify settings over WAN",
            )

        # 2. Over WAN, clients can update detection parameter tuning, target objects, cached models, cameras, and user access
        allowed_wan_fields = {
            "confidence_threshold",
            "class_confidence_thresholds",
            "target_classes",
            "sample_fps",
            "min_clip_keyframes",
            "vid_stride",
            "cameras",
            "allowed_google_emails",
            "admin_google_emails",
            "yolo_model",
            "yolo_model_sha256",
        }

        # Check for actual modifications to LAN-only fields (ignoring unchanged LAN values in payload)
        disallowed_changes = set()
        for key, new_val in updates.items():
            if key not in allowed_wan_fields:
                old_val = getattr(cfg, key, None)
                if _settings_values_differ(old_val, new_val):
                    disallowed_changes.add(key)

        if disallowed_changes:
            logger.warning(
                f"[AUDIT] [AUTH_DENIED] User {current_user.email} attempted to update disallowed fields over WAN: {disallowed_changes}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Only detection tuning, cached YOLO model, camera, and user access settings can be modified over WAN. The following settings can only be changed over local LAN: {', '.join(sorted(disallowed_changes))}",
            )

        # 3. Model activation over WAN: only locally cached models are permitted
        if "yolo_model" in updates:
            new_model = updates["yolo_model"]
            if new_model and new_model != cfg.yolo_model:
                models_dir = Path(cfg.models_dir) if hasattr(cfg, "models_dir") else Path("/models")
                candidate_path = models_dir / new_model
                if not candidate_path.is_file():
                    logger.warning(
                        f"[AUDIT] [AUTH_DENIED] User {current_user.email} attempted to activate uncached model '{new_model}' over WAN"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Model '{new_model}' is not cached locally in {models_dir}. Over WAN, only locally cached models can be activated.",
                    )

        # 4. Deleting cameras is strictly prohibited over WAN
        if "cameras" in updates:
            existing_serials = {cam.serial.strip().upper() for cam in cfg.cameras if cam.serial}
            new_cameras = updates.get("cameras")
            if isinstance(new_cameras, list):
                new_serials = {
                    str(getattr(c, "serial", None) or (c.get("serial", "") if isinstance(c, dict) else "")).strip().upper()
                    for c in new_cameras
                    if (getattr(c, "serial", None) or (isinstance(c, dict) and c.get("serial")))
                }
                deleted_serials = existing_serials - new_serials
                if deleted_serials:
                    logger.warning(
                        f"[api/settings] user {current_user.email} attempted to delete cameras over WAN: {deleted_serials}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Deleting cameras is only allowed when connected over local LAN. Missing camera serial(s): {', '.join(sorted(deleted_serials))}",
                    )

    # Validate and synchronize user management lists if provided
    if "allowed_google_emails" in updates or "admin_google_emails" in updates:
        allowed = [
            e.strip().lower()
            for e in updates.get("allowed_google_emails", cfg.allowed_google_emails or [])
            if e and e.strip()
        ]
        admins = [
            e.strip().lower()
            for e in updates.get("admin_google_emails", cfg.admin_google_emails or [])
            if e and e.strip()
        ]

        # Deduplicate while preserving order
        allowed = list(dict.fromkeys(allowed))
        admins = list(dict.fromkeys(admins))

        # 1. Any admin must automatically be in allowed_emails
        for admin_email in admins:
            if admin_email not in allowed:
                allowed.append(admin_email)

        # 2. If allowed emails is configured, at least one admin must exist
        if allowed and not admins:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one administrator account must be designated when user whitelist is enabled.",
            )

        # 3. WAN self-lockout guard: current admin cannot remove or demote themselves over WAN
        if not is_lan and current_user and current_user.email:
            current_email_lower = current_user.email.strip().lower()
            if current_email_lower not in admins:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot remove or demote your own administrator account while connected over WAN.",
                )

        updates["allowed_google_emails"] = allowed
        updates["admin_google_emails"] = admins

    try:
        new_settings = cfg.update_with(updates)
        if new_settings.settings_config_path.suffix in (".yaml", ".yml"):
            new_settings.save_to_yaml()
        else:
            new_settings.save_to_json()

        # Update Caddyfile so Caddy proxy hot-reloads configuration
        new_settings.save_caddyfile()

        # Update in-memory FastAPI app state and pipeline components
        request.app.state.settings = new_settings
        if hasattr(request.app.state, "pipeline") and request.app.state.pipeline:
            request.app.state.pipeline.update_settings(new_settings)

        logger.info(f"[AUDIT] [ADMIN_ACTION] Settings updated by {current_user.email}: {list(updates.keys())}")
        return {
            "status": "updated",
            "updated_fields": list(updates.keys()),
            "settings": new_settings.export_editable_dict(),
        }
    except Exception as exc:
        logger.error(f"[api/settings] failed to update settings: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid settings configuration: {exc}",
        )


class RawSettingsUpdateRequest(BaseModel):
    raw_yaml: str


@router.put("/raw", summary="Update application settings from raw YAML string")
async def update_settings_raw(
    body: RawSettingsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    cfg: Settings = request.app.state.settings

    # WAN Security Policy Enforcement
    is_lan = is_lan_client(request, cfg)
    if not is_lan:
        if not is_admin_user(current_user, cfg):
            logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} attempted to update raw YAML over WAN")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required to modify settings over WAN",
            )

    # Parse YAML safely
    try:
        parsed_yaml = yaml.safe_load(body.raw_yaml)
    except yaml.YAMLError as exc:
        logger.warning(f"[api/settings] Invalid YAML syntax: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid YAML syntax: {exc}",
        )

    if not isinstance(parsed_yaml, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parsed YAML must produce a dictionary of settings.",
        )

    # Validate against SettingsUpdateRequest
    try:
        validated_request = SettingsUpdateRequest(**parsed_yaml)
    except Exception as exc:
        logger.warning(f"[api/settings] YAML validation error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Settings validation error: {exc}",
        )

    return await update_settings(validated_request, request, current_user)

