#!/usr/bin/env python3
"""
Apple Watch -> capture card -> OpenAI -> Apple Watch bridge.

Run this on the Mac/PC connected to your capture card. An Apple Watch Shortcut,
companion iPhone app, or any HTTP client can call /watch/trigger or
/watch/signal to start the flow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from openai import OpenAI
from pydantic import BaseModel, Field, HttpUrl


load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("watch_bridge")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8787)
    bridge_token: str = os.getenv("BRIDGE_TOKEN", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    default_prompt: str = os.getenv(
        "DEFAULT_PROMPT",
        "Analyze the captured presentation screen and provide a concise response.",
    )
    image_detail: str = os.getenv("IMAGE_DETAIL", "auto")
    max_output_tokens: int = _env_int("MAX_OUTPUT_TOKENS", 700)
    capture_device_index: int = _env_int("CAPTURE_DEVICE_INDEX", 0)
    capture_device_names: str = os.getenv("CAPTURE_DEVICE_NAMES", "")
    capture_backend: str = os.getenv("CAPTURE_BACKEND", "auto")
    capture_width: int = _env_int("CAPTURE_WIDTH", 1920)
    capture_height: int = _env_int("CAPTURE_HEIGHT", 1080)
    capture_fps: float = _env_float("CAPTURE_FPS", 0.0)
    capture_fourcc: str = os.getenv("CAPTURE_FOURCC", "")
    capture_buffer_size: int = _env_int("CAPTURE_BUFFER_SIZE", 1)
    capture_strict_mode: bool = _env_bool("CAPTURE_STRICT_MODE", False)
    capture_mode_tolerance_pixels: int = _env_int("CAPTURE_MODE_TOLERANCE_PIXELS", 8)
    capture_warmup_frames: int = _env_int("CAPTURE_WARMUP_FRAMES", 4)
    capture_timeout_seconds: float = _env_float("CAPTURE_TIMEOUT_SECONDS", 4.0)
    capture_read_retries: int = _env_int("CAPTURE_READ_RETRIES", 3)
    capture_read_retry_pause_seconds: float = _env_float(
        "CAPTURE_READ_RETRY_PAUSE_SECONDS",
        1.5,
    )
    capture_subprocess_fallback_enabled: bool = _env_bool(
        "CAPTURE_SUBPROCESS_FALLBACK_ENABLED",
        True,
    )
    capture_subprocess_timeout_seconds: float = _env_float(
        "CAPTURE_SUBPROCESS_TIMEOUT_SECONDS",
        30.0,
    )
    capture_stale_watchdog_enabled: bool = _env_bool(
        "CAPTURE_STALE_WATCHDOG_ENABLED",
        True,
    )
    stale_frame_seconds: float = _env_float("STALE_FRAME_SECONDS", 20.0)
    stale_frame_signature_size: int = _env_int("STALE_FRAME_SIGNATURE_SIZE", 24)
    stale_frame_diff_threshold: float = _env_float("STALE_FRAME_DIFF_THRESHOLD", 2.0)
    video_reactivation_attempts: int = _env_int("VIDEO_REACTIVATION_ATTEMPTS", 1)
    video_reactivation_pause_seconds: float = _env_float(
        "VIDEO_REACTIVATION_PAUSE_SECONDS",
        1.0,
    )
    image_crop_left: int = _env_int("IMAGE_CROP_LEFT", 0)
    image_crop_top: int = _env_int("IMAGE_CROP_TOP", 0)
    image_crop_width: int = _env_int("IMAGE_CROP_WIDTH", 0)
    image_crop_height: int = _env_int("IMAGE_CROP_HEIGHT", 0)
    image_output_width: int = _env_int("IMAGE_OUTPUT_WIDTH", 0)
    image_output_height: int = _env_int("IMAGE_OUTPUT_HEIGHT", 0)
    jpeg_quality: int = _env_int("JPEG_QUALITY", 92)
    request_log_enabled: bool = _env_bool("REQUEST_LOG_ENABLED", True)
    request_log_root: str = os.getenv("REQUEST_LOG_ROOT", "request_logs")
    watch_callback_url: str = os.getenv("WATCH_CALLBACK_URL", "")
    callback_timeout_seconds: float = _env_float("CALLBACK_TIMEOUT_SECONDS", 6.0)


settings = Settings()


@dataclass(frozen=True)
class CaptureResult:
    jpeg_bytes: bytes
    raw_jpeg_bytes: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RequestLog:
    request_id: str
    path: Path


@dataclass(frozen=True)
class CaptureDeviceSelection:
    index: int
    metadata: dict[str, Any]


@dataclass(frozen=True)
class OpenAIResult:
    text: str
    response_id: str | None
    previous_response_id: str | None
    turn_count: int


@dataclass
class OpenAISessionState:
    previous_response_id: str | None = None
    turn_count: int = 0


@dataclass
class CaptureWatchdogState:
    device_index: int | None = None
    signature: Any | None = None
    unchanged_since: float | None = None
    last_checked_at: float | None = None
    reactivation_count: int = 0


class WatchSignal(BaseModel):
    prompt: str | None = Field(
        default=None,
        description="Optional prompt override for this capture.",
    )
    callback_url: HttpUrl | None = Field(
        default=None,
        description="Optional endpoint to POST the model response to.",
    )
    capture_device_index: int | None = Field(
        default=None,
        description="Optional OpenCV camera/capture-card device index override.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, capture a frame but skip the OpenAI request.",
    )


class BridgeResult(BaseModel):
    ok: bool
    text: str
    model: str
    captured_at: str
    latency_ms: int
    openai_session: dict[str, Any] | None = None
    capture: dict[str, Any] | None = None
    callback: dict[str, Any] | None = None


capture_lock = asyncio.Lock()
capture_watchdog_state = CaptureWatchdogState()
openai_session_lock = threading.Lock()
openai_session_state = OpenAISessionState()
last_result: BridgeResult | None = None
capture_manager: Any | None = None


def require_token(
    authorization: str | None = Header(default=None),
    x_bridge_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    if not settings.bridge_token:
        return

    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()

    supplied = bearer or x_bridge_token or token
    if supplied != settings.bridge_token:
        raise HTTPException(status_code=401, detail="Missing or invalid bridge token")


def load_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "opencv-python is required for capture-card screenshots. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    return cv2


def resolve_capture_backend(cv2: Any) -> tuple[int, str]:
    requested = settings.capture_backend.strip().lower()
    if requested == "auto":
        system = platform.system()
        if system == "Darwin":
            requested = "avfoundation"
        elif system == "Windows":
            requested = "dshow"
        elif system == "Linux":
            requested = "v4l2"
        else:
            requested = "any"

    mapping = {
        "any": "CAP_ANY",
        "default": "CAP_ANY",
        "avfoundation": "CAP_AVFOUNDATION",
        "dshow": "CAP_DSHOW",
        "directshow": "CAP_DSHOW",
        "msmf": "CAP_MSMF",
        "v4l2": "CAP_V4L2",
    }

    if requested not in mapping:
        allowed = ", ".join(sorted(mapping))
        raise RuntimeError(f"Unsupported CAPTURE_BACKEND={requested!r}. Use one of: {allowed}")

    constant_name = mapping[requested]
    if not hasattr(cv2, constant_name):
        logger.warning(
            "OpenCV does not expose %s on this platform; falling back to CAP_ANY",
            constant_name,
        )
        return cv2.CAP_ANY, "any"

    return getattr(cv2, constant_name), requested


def normalize_device_name(name: str) -> str:
    return " ".join(name.casefold().strip().split())


def capture_device_name_preferences() -> list[str]:
    raw = settings.capture_device_names.strip()
    if not raw:
        return []

    for separator in (";", "|"):
        raw = raw.replace(separator, ",")
    return [name.strip() for name in raw.split(",") if name.strip()]


def enumerate_macos_video_devices() -> list[dict[str, Any]]:
    if platform.system() != "Darwin":
        return []

    try:
        import AVFoundation
    except ImportError:
        logger.warning(
            "CAPTURE_DEVICE_NAMES is configured, but pyobjc-framework-AVFoundation "
            "is not installed; falling back to CAPTURE_DEVICE_INDEX."
        )
        return []

    devices = AVFoundation.AVCaptureDevice.devicesWithMediaType_(
        AVFoundation.AVMediaTypeVideo,
    )
    return [
        {
            "index": index,
            "name": str(device.localizedName()),
            "unique_id": str(device.uniqueID()),
            "source": "avfoundation",
        }
        for index, device in enumerate(devices)
    ]


def enumerate_named_capture_devices() -> list[dict[str, Any]]:
    if platform.system() == "Darwin":
        return enumerate_macos_video_devices()

    logger.warning(
        "CAPTURE_DEVICE_NAMES is only implemented for macOS right now; "
        "falling back to CAPTURE_DEVICE_INDEX on %s.",
        platform.system(),
    )
    return []


def choose_capture_device_by_name(
    preferences: list[str],
    devices: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not preferences or not devices:
        return None

    normalized_devices = [
        (device, normalize_device_name(str(device.get("name", ""))))
        for device in devices
    ]

    for preferred in preferences:
        preferred_normalized = normalize_device_name(preferred)
        for device, device_name in normalized_devices:
            if device_name == preferred_normalized:
                return device

        for device, device_name in normalized_devices:
            if preferred_normalized in device_name:
                return device

    return None


class CaptureDeviceManager:
    """OBS-like lifecycle for a capture-card input.

    OBS deactivate/activate tears down and rebuilds the source's device graph.
    Here, the equivalent is releasing the OpenCV VideoCapture handle and then
    creating a fresh one with the configured backend and device settings.
    """

    def __init__(self) -> None:
        self.cv2 = load_cv2()
        self.cap: Any | None = None
        self.device_index: int | None = None
        self.backend_name = ""
        self.opened_at: float | None = None
        self.device_selection: dict[str, Any] | None = None

    def resolve_device(self, requested_index: int | None = None) -> CaptureDeviceSelection:
        if requested_index is not None:
            return CaptureDeviceSelection(
                index=requested_index,
                metadata={
                    "source": "request_index",
                    "index": requested_index,
                    "configured_names": capture_device_name_preferences(),
                },
            )

        preferences = capture_device_name_preferences()
        if preferences:
            devices = enumerate_named_capture_devices()
            selected = choose_capture_device_by_name(preferences, devices)
            if selected is not None:
                return CaptureDeviceSelection(
                    index=int(selected["index"]),
                    metadata={
                        "source": selected.get("source", "device_name"),
                        "index": int(selected["index"]),
                        "name": selected.get("name"),
                        "unique_id": selected.get("unique_id"),
                        "configured_names": preferences,
                        "available_devices": [
                            {
                                "index": device.get("index"),
                                "name": device.get("name"),
                                "unique_id": device.get("unique_id"),
                            }
                            for device in devices
                        ],
                    },
                )

            logger.warning(
                "No capture device matched CAPTURE_DEVICE_NAMES=%s. Available devices: %s. "
                "Falling back to CAPTURE_DEVICE_INDEX=%s.",
                preferences,
                [device.get("name") for device in devices],
                settings.capture_device_index,
            )

        return CaptureDeviceSelection(
            index=settings.capture_device_index,
            metadata={
                "source": "fallback_index",
                "index": settings.capture_device_index,
                "configured_names": preferences,
            },
        )

    def is_active_for(self, index: int) -> bool:
        return (
            self.cap is not None
            and self.device_index == index
            and bool(self.cap.isOpened())
        )

    def activate(self, index: int, force: bool = False) -> dict[str, Any]:
        if self.is_active_for(index) and not force:
            return {
                "ok": True,
                "device_index": index,
                "backend": self.backend_name,
                "already_active": True,
                "properties": self.actual_properties(),
            }

        self.deactivate()

        backend_code, backend_name = resolve_capture_backend(self.cv2)
        logger.info(
            "Activating capture device index %s with backend %s",
            index,
            backend_name,
        )

        if backend_code == self.cv2.CAP_ANY:
            cap = self.cv2.VideoCapture(index)
        else:
            cap = self.cv2.VideoCapture(index, backend_code)

        if not cap.isOpened():
            cap.release()
            raise RuntimeError(
                f"Could not open capture device index {index} with backend "
                f"{backend_name}. Try CAPTURE_DEVICE_INDEX or CAPTURE_BACKEND."
            )

        self.cap = cap
        self.device_index = index
        self.backend_name = backend_name
        self.opened_at = time.monotonic()
        self.device_selection = None
        try:
            self.configure_active_device()
            self.warm_up()
        except Exception:
            self.deactivate()
            raise

        return {
            "ok": True,
            "device_index": index,
            "backend": backend_name,
            "already_active": False,
            "properties": self.actual_properties(),
        }

    def deactivate(self) -> dict[str, Any]:
        was_active = self.cap is not None
        previous_index = self.device_index
        previous_backend = self.backend_name

        if self.cap is not None:
            logger.info(
                "Deactivating capture device index %s with backend %s",
                previous_index,
                previous_backend or "unknown",
            )
            self.cap.release()

        self.cap = None
        self.device_index = None
        self.backend_name = ""
        self.opened_at = None
        self.device_selection = None

        return {
            "ok": True,
            "was_active": was_active,
            "device_index": previous_index,
            "backend": previous_backend or None,
        }

    def reactivate(
        self,
        requested_index: int | None = None,
        *,
        force_index: int | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        attempts = max(1, settings.video_reactivation_attempts)
        pause = max(0.0, settings.video_reactivation_pause_seconds)
        last_error = ""
        deactivation = self.deactivate()
        last_selection: CaptureDeviceSelection | None = None

        for attempt in range(1, attempts + 1):
            selection = (
                CaptureDeviceSelection(
                    index=force_index,
                    metadata={
                        "source": "forced_reactivation_index",
                        "index": force_index,
                        "configured_names": capture_device_name_preferences(),
                    },
                )
                if force_index is not None
                else self.resolve_device(requested_index)
            )
            last_selection = selection
            index = selection.index

            logger.warning(
                "Reactivating capture device index %s, attempt %s/%s, selection=%s",
                index,
                attempt,
                attempts,
                selection.metadata,
            )
            if pause:
                time.sleep(pause)

            try:
                activation = self.activate(index, force=True)
            except Exception as exc:  # noqa: BLE001 - retry with the next attempt.
                last_error = str(exc)
                logger.warning("Video input reactivation failed: %s", exc)
                continue

            capture_watchdog_state.reactivation_count += 1
            return {
                "ok": True,
                "device_index": index,
                "attempts": attempt,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "deactivation": deactivation,
                "device_selection": selection.metadata,
                "activation": activation,
            }

        return {
            "ok": False,
            "device_index": last_selection.index if last_selection else requested_index,
            "attempts": attempts,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "deactivation": deactivation,
            "device_selection": last_selection.metadata if last_selection else None,
            "error": last_error or "Video input reactivation failed",
        }

    def configure_active_device(self) -> None:
        if self.cap is None:
            return

        fourcc = settings.capture_fourcc.strip()
        if fourcc:
            if len(fourcc) != 4:
                raise RuntimeError("CAPTURE_FOURCC must be exactly 4 characters")
            self.cap.set(
                self.cv2.CAP_PROP_FOURCC,
                self.cv2.VideoWriter_fourcc(*fourcc),
            )

        self.cap.set(self.cv2.CAP_PROP_FRAME_WIDTH, settings.capture_width)
        self.cap.set(self.cv2.CAP_PROP_FRAME_HEIGHT, settings.capture_height)

        if settings.capture_fps > 0:
            self.cap.set(self.cv2.CAP_PROP_FPS, settings.capture_fps)

        if settings.capture_buffer_size > 0 and hasattr(self.cv2, "CAP_PROP_BUFFERSIZE"):
            self.cap.set(self.cv2.CAP_PROP_BUFFERSIZE, settings.capture_buffer_size)

        self.validate_requested_mode()

    def requested_properties(self) -> dict[str, Any]:
        return {
            "width": settings.capture_width,
            "height": settings.capture_height,
            "fps": settings.capture_fps or None,
            "fourcc": settings.capture_fourcc.strip() or None,
        }

    def validate_requested_mode(self) -> None:
        if self.cap is None:
            return

        actual = self.actual_properties()
        tolerance = max(0, settings.capture_mode_tolerance_pixels)
        width_delta = abs(actual.get("width", 0) - settings.capture_width)
        height_delta = abs(actual.get("height", 0) - settings.capture_height)

        if width_delta <= tolerance and height_delta <= tolerance:
            return

        message = (
            "Capture device did not accept requested mode: "
            f"requested={self.requested_properties()}, actual={actual}"
        )
        if settings.capture_strict_mode:
            raise RuntimeError(message)
        logger.warning(message)

    def warm_up(self) -> None:
        if self.cap is None:
            return

        for _ in range(max(0, settings.capture_warmup_frames)):
            self.cap.read()

    def actual_fourcc(self) -> str | None:
        if self.cap is None:
            return None

        value = int(self.cap.get(self.cv2.CAP_PROP_FOURCC))
        if value <= 0:
            return None

        chars = [chr((value >> (8 * i)) & 0xFF) for i in range(4)]
        text = "".join(chars)
        return text if text.strip("\x00") else None

    def actual_properties(self) -> dict[str, Any]:
        if self.cap is None:
            return {}

        return {
            "width": round(self.cap.get(self.cv2.CAP_PROP_FRAME_WIDTH)),
            "height": round(self.cap.get(self.cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(self.cap.get(self.cv2.CAP_PROP_FPS), 3),
            "fourcc": self.actual_fourcc(),
        }

    def read_frame(self, selection: CaptureDeviceSelection) -> tuple[Any, dict[str, Any]]:
        requested_index = (
            selection.index
            if selection.metadata.get("source") == "request_index"
            else None
        )
        index = selection.index
        activation = self.activate(index)
        read_attempts: list[dict[str, Any]] = [
            {
                "kind": "initial",
                "device_index": index,
                "selection": selection.metadata,
            }
        ]

        frame = self._read_frame_from_active()
        read_failure_reactivation = None
        retry_count = max(0, settings.capture_read_retries)
        retry_pause = max(0.0, settings.capture_read_retry_pause_seconds)

        for retry_number in range(1, retry_count + 1):
            if frame is not None:
                break

            logger.warning(
                "Timed out reading from capture device index %s; retrying %s/%s "
                "after full reactivation.",
                index,
                retry_number,
                retry_count,
            )
            if retry_pause:
                time.sleep(retry_pause)

            read_failure_reactivation = self.reactivate(requested_index)
            read_attempts.append(
                {
                    "kind": "reactivation_retry",
                    "retry_number": retry_number,
                    "result": read_failure_reactivation,
                }
            )
            if not read_failure_reactivation.get("ok"):
                continue

            reactivated_index = read_failure_reactivation.get("device_index")
            if isinstance(reactivated_index, int):
                index = reactivated_index
                selection = CaptureDeviceSelection(
                    index=index,
                    metadata=read_failure_reactivation.get("device_selection") or selection.metadata,
                )
            frame = self._read_frame_from_active()

        if frame is None:
            self.deactivate()
            raise RuntimeError(f"Timed out waiting for a frame from device index {index}")

        height, width = frame.shape[:2]
        self.validate_frame_shape(width, height)
        metadata = {
            "device_index": index,
            "backend": self.backend_name,
            "activation": activation,
            "read_failure_reactivation": read_failure_reactivation,
            "read_attempts": read_attempts,
            "selection": selection.metadata,
            "requested_mode": self.requested_properties(),
            "raw_frame": {"width": width, "height": height},
        }
        return frame, metadata

    def validate_frame_shape(self, width: int, height: int) -> None:
        tolerance = max(0, settings.capture_mode_tolerance_pixels)
        width_delta = abs(width - settings.capture_width)
        height_delta = abs(height - settings.capture_height)

        if width_delta <= tolerance and height_delta <= tolerance:
            return

        message = (
            "Capture device produced a different frame size than requested: "
            f"requested={settings.capture_width}x{settings.capture_height}, "
            f"actual={width}x{height}. The device/backend may have rejected "
            "the requested mode."
        )
        if settings.capture_strict_mode:
            raise RuntimeError(message)
        logger.warning(message)

    def _read_frame_from_active(self) -> Any | None:
        if self.cap is None:
            return None

        deadline = time.monotonic() + settings.capture_timeout_seconds
        frame = None

        for _ in range(max(1, settings.capture_warmup_frames)):
            ok, candidate = self.cap.read()
            if ok:
                frame = candidate
            if time.monotonic() > deadline:
                break

        while frame is None and time.monotonic() <= deadline:
            ok, candidate = self.cap.read()
            if ok:
                frame = candidate
            else:
                time.sleep(0.05)

        return frame


def get_capture_manager() -> CaptureDeviceManager:
    global capture_manager
    if capture_manager is None:
        capture_manager = CaptureDeviceManager()
    return capture_manager


def frame_signature(cv2: Any, frame: Any) -> Any:
    size = max(4, settings.stale_frame_signature_size)
    resized = cv2.resize(frame, (size, size), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)


def frame_signature_difference(signature_a: Any, signature_b: Any) -> float:
    return float(abs(signature_a.astype("int16") - signature_b.astype("int16")).mean())


def reset_watchdog_signature(cv2: Any, frame: Any, index: int) -> None:
    now = time.monotonic()
    capture_watchdog_state.device_index = index
    capture_watchdog_state.signature = frame_signature(cv2, frame)
    capture_watchdog_state.unchanged_since = now
    capture_watchdog_state.last_checked_at = now


def evaluate_frame_staleness(cv2: Any, frame: Any, index: int) -> dict[str, Any]:
    if (
        not settings.capture_stale_watchdog_enabled
        or settings.stale_frame_seconds <= 0
    ):
        return {"enabled": False, "reactivation_needed": False}

    now = time.monotonic()
    signature = frame_signature(cv2, frame)
    state = capture_watchdog_state

    if state.device_index != index or state.signature is None:
        state.device_index = index
        state.signature = signature
        state.unchanged_since = now
        state.last_checked_at = now
        return {
            "enabled": True,
            "same_as_previous": False,
            "difference": None,
            "unchanged_seconds": 0.0,
            "reactivation_needed": False,
            "reactivation_count": state.reactivation_count,
        }

    difference = frame_signature_difference(signature, state.signature)
    same_as_previous = difference <= settings.stale_frame_diff_threshold

    if same_as_previous:
        if state.unchanged_since is None:
            state.unchanged_since = now
        unchanged_seconds = now - state.unchanged_since
    else:
        state.signature = signature
        state.unchanged_since = now
        unchanged_seconds = 0.0

    state.last_checked_at = now
    reactivation_needed = (
        same_as_previous and unchanged_seconds >= settings.stale_frame_seconds
    )

    return {
        "enabled": True,
        "same_as_previous": same_as_previous,
        "difference": round(difference, 3),
        "unchanged_seconds": round(unchanged_seconds, 3),
        "reactivation_needed": reactivation_needed,
        "reactivation_count": state.reactivation_count,
    }


def deactivate_and_reactivate_video_input(device_index: int | None = None) -> dict[str, Any]:
    manager = get_capture_manager()
    return manager.reactivate(device_index)


def apply_image_crop_and_resize(cv2: Any, frame: Any) -> tuple[Any, dict[str, Any]]:
    raw_height, raw_width = frame.shape[:2]
    metadata: dict[str, Any] = {
        "source_width": raw_width,
        "source_height": raw_height,
        "crop": None,
        "resize": None,
    }

    crop_left = settings.image_crop_left
    crop_top = settings.image_crop_top
    crop_width = settings.image_crop_width
    crop_height = settings.image_crop_height

    crop_requested = any((crop_left, crop_top, crop_width, crop_height))
    if crop_requested:
        if crop_left < 0 or crop_top < 0:
            raise RuntimeError("IMAGE_CROP_LEFT and IMAGE_CROP_TOP must be >= 0")

        if crop_left >= raw_width or crop_top >= raw_height:
            raise RuntimeError(
                "Configured image crop starts outside the captured frame "
                f"({raw_width}x{raw_height})"
            )

        if crop_width <= 0:
            crop_width = raw_width - crop_left
        if crop_height <= 0:
            crop_height = raw_height - crop_top

        if crop_width <= 0 or crop_height <= 0:
            raise RuntimeError("IMAGE_CROP_WIDTH and IMAGE_CROP_HEIGHT must be > 0")

        crop_right = crop_left + crop_width
        crop_bottom = crop_top + crop_height
        if crop_right > raw_width or crop_bottom > raw_height:
            raise RuntimeError(
                "Configured image crop exceeds captured frame bounds: "
                f"crop=({crop_left},{crop_top},{crop_width},{crop_height}), "
                f"frame={raw_width}x{raw_height}"
            )

        frame = frame[crop_top:crop_bottom, crop_left:crop_right]
        metadata["crop"] = {
            "left": crop_left,
            "top": crop_top,
            "width": crop_width,
            "height": crop_height,
        }

    height, width = frame.shape[:2]
    output_width = settings.image_output_width
    output_height = settings.image_output_height

    if output_width or output_height:
        if output_width < 0 or output_height < 0:
            raise RuntimeError("IMAGE_OUTPUT_WIDTH and IMAGE_OUTPUT_HEIGHT must be >= 0")

        if output_width == 0:
            output_width = round(width * (output_height / height))
        if output_height == 0:
            output_height = round(height * (output_width / width))

        if output_width <= 0 or output_height <= 0:
            raise RuntimeError("IMAGE_OUTPUT_WIDTH and IMAGE_OUTPUT_HEIGHT must resolve to > 0")

        interpolation = (
            cv2.INTER_AREA
            if output_width <= width and output_height <= height
            else cv2.INTER_CUBIC
        )
        frame = cv2.resize(frame, (output_width, output_height), interpolation=interpolation)
        metadata["resize"] = {
            "width": output_width,
            "height": output_height,
            "mode": "exact" if settings.image_output_width and settings.image_output_height else "keep_aspect",
        }

    final_height, final_width = frame.shape[:2]
    metadata["final_width"] = final_width
    metadata["final_height"] = final_height
    return frame, metadata


def encode_jpeg(cv2: Any, frame: Any, quality: int) -> bytes:
    jpeg_quality = max(1, min(100, quality))
    ok, encoded = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
    )
    if not ok:
        raise RuntimeError("Captured a frame, but JPEG encoding failed")
    return encoded.tobytes()


def capture_subprocess_worker_enabled() -> bool:
    return os.getenv("WATCH_BRIDGE_CAPTURE_WORKER") != "1"


def capture_jpeg_frame_result_subprocess(
    device_index: int | None,
    original_error: Exception,
) -> CaptureResult:
    if (
        not settings.capture_subprocess_fallback_enabled
        or not capture_subprocess_worker_enabled()
    ):
        raise original_error

    started = time.monotonic()
    logger.warning(
        "In-process capture failed; trying fresh subprocess capture: %s",
        original_error,
    )

    with tempfile.TemporaryDirectory(prefix="watch_bridge_capture_") as tmpdir:
        output_dir = Path(tmpdir)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "capture-worker",
            "--output-dir",
            str(output_dir),
        ]
        if device_index is not None:
            command.extend(["--device-index", str(device_index)])

        env = os.environ.copy()
        env["WATCH_BRIDGE_CAPTURE_WORKER"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parent),
                env=env,
                capture_output=True,
                text=True,
                timeout=settings.capture_subprocess_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "In-process capture failed, and subprocess capture timed out. "
                f"Original error: {original_error}."
            ) from exc

        if completed.returncode != 0:
            raise RuntimeError(
                "In-process capture failed, and subprocess capture also failed. "
                f"Original error: {original_error}. "
                f"Subprocess return code: {completed.returncode}. "
                f"Subprocess stderr: {completed.stderr[-4000:]}"
            ) from original_error

        raw_jpeg_bytes = (output_dir / "raw_capture.jpg").read_bytes()
        jpeg_bytes = (output_dir / "sent_to_openai.jpg").read_bytes()
        metadata = json.loads((output_dir / "capture_metadata.json").read_text(encoding="utf-8"))
        metadata["subprocess_fallback"] = {
            "used": True,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "original_error": str(original_error),
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
        return CaptureResult(
            jpeg_bytes=jpeg_bytes,
            raw_jpeg_bytes=raw_jpeg_bytes,
            metadata=metadata,
        )


def capture_jpeg_frame_result(device_index: int | None = None) -> CaptureResult:
    manager = get_capture_manager()
    selection = manager.resolve_device(device_index)
    index = selection.index
    manager.device_selection = selection.metadata
    cv2 = manager.cv2
    try:
        frame, read_metadata = manager.read_frame(selection)
    except Exception as exc:
        manager.deactivate()
        return capture_jpeg_frame_result_subprocess(device_index, exc)

    index = int(read_metadata["device_index"])
    selection = CaptureDeviceSelection(
        index=index,
        metadata=read_metadata.get("selection") or selection.metadata,
    )
    stale_watchdog = evaluate_frame_staleness(cv2, frame, index)

    if stale_watchdog.get("reactivation_needed"):
        reset_result = manager.reactivate(device_index)
        stale_watchdog["reactivation"] = reset_result
        if reset_result.get("ok"):
            reactivated_index = reset_result.get("device_index")
            reactivated_selection = CaptureDeviceSelection(
                index=int(reactivated_index),
                metadata=reset_result.get("device_selection") or selection.metadata,
            )
            frame, read_metadata = manager.read_frame(reactivated_selection)
            index = int(read_metadata["device_index"])
            selection = CaptureDeviceSelection(
                index=index,
                metadata=read_metadata.get("selection") or reactivated_selection.metadata,
            )
            reset_watchdog_signature(cv2, frame, index)
            stale_watchdog["recaptured_after_reactivation"] = True
    else:
        stale_watchdog["recaptured_after_reactivation"] = False

    raw_jpeg_bytes = encode_jpeg(cv2, frame, settings.jpeg_quality)
    processed_frame, image_metadata = apply_image_crop_and_resize(cv2, frame)
    jpeg_quality = max(1, min(100, settings.jpeg_quality))
    jpeg_bytes = encode_jpeg(cv2, processed_frame, jpeg_quality)
    metadata = {
        "device_index": index,
        "device_selection": selection.metadata,
        "device": read_metadata,
        "image": image_metadata,
        "jpeg_quality": jpeg_quality,
        "jpeg_bytes": len(jpeg_bytes),
        "stale_watchdog": stale_watchdog,
    }
    return CaptureResult(
        jpeg_bytes=jpeg_bytes,
        raw_jpeg_bytes=raw_jpeg_bytes,
        metadata=metadata,
    )


def capture_jpeg_frame(device_index: int | None = None) -> bytes:
    return capture_jpeg_frame_result(device_index).jpeg_bytes


def jpeg_to_data_url(jpeg_bytes: bytes) -> str:
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def create_request_log(captured_at: str) -> RequestLog | None:
    if not settings.request_log_enabled:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    request_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"
    root = Path(settings.request_log_root).expanduser()
    request_dir = root / request_id
    request_dir.mkdir(parents=True, exist_ok=False)

    write_json(
        request_dir / "request.json",
        {
            "request_id": request_id,
            "captured_at": captured_at,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return RequestLog(request_id=request_id, path=request_dir)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def save_request_prompt(
    request_log: RequestLog | None,
    prompt: str,
    signal: WatchSignal,
) -> None:
    if request_log is None:
        return

    write_text(request_log.path / "prompt.txt", prompt)
    write_json(
        request_log.path / "signal.json",
        {
            "request_id": request_log.request_id,
            "prompt": prompt,
            "signal": signal.model_dump(mode="json"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def save_capture_artifacts(
    request_log: RequestLog | None,
    capture_result: CaptureResult,
) -> None:
    if request_log is None:
        return

    (request_log.path / "raw_capture.jpg").write_bytes(capture_result.raw_jpeg_bytes)
    (request_log.path / "sent_to_openai.jpg").write_bytes(capture_result.jpeg_bytes)
    write_json(request_log.path / "capture_metadata.json", capture_result.metadata)


def save_response_artifacts(
    request_log: RequestLog | None,
    response_text: str,
    result: BridgeResult,
) -> None:
    if request_log is None:
        return

    write_text(request_log.path / "response.txt", response_text)
    write_json(request_log.path / "result.json", result.model_dump(mode="json"))


def save_error_artifacts(
    request_log: RequestLog | None,
    exc: Exception,
) -> None:
    if request_log is None:
        return

    write_text(request_log.path / "error.txt", f"{type(exc).__name__}: {exc}\n")
    write_text(request_log.path / "traceback.txt", traceback.format_exc())


def extract_response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text.strip()

    # Fallback for SDK/API response shapes that do not expose output_text.
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            value = getattr(content, "text", None)
            if value:
                chunks.append(value)
    return "\n".join(chunks).strip()


def ask_openai(prompt: str, image_data_url: str) -> OpenAIResult:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=settings.openai_api_key)

    with openai_session_lock:
        previous_response_id = openai_session_state.previous_response_id
        request: dict[str, Any] = {
            "model": settings.openai_model,
            "max_output_tokens": settings.max_output_tokens,
            "store": True,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": settings.image_detail,
                        },
                    ],
                }
            ],
        }

        if previous_response_id:
            request["previous_response_id"] = previous_response_id

        response = client.responses.create(**request)
        response_id = getattr(response, "id", None)
        if response_id:
            openai_session_state.previous_response_id = response_id
            openai_session_state.turn_count += 1
        turn_count = openai_session_state.turn_count

    return OpenAIResult(
        text=extract_response_text(response),
        response_id=response_id,
        previous_response_id=previous_response_id,
        turn_count=turn_count,
    )


def openai_session_metadata(result: OpenAIResult | None) -> dict[str, Any]:
    return {
        "enabled": True,
        "turn_count": result.turn_count if result else openai_session_state.turn_count,
        "previous_response_id_used": result.previous_response_id if result else None,
        "response_id": result.response_id if result else None,
        "resets_when": "python_process_restarts",
    }


def dry_run_openai_session_metadata() -> dict[str, Any]:
    with openai_session_lock:
        turn_count = openai_session_state.turn_count
    return {
        "enabled": True,
        "turn_count": turn_count,
        "previous_response_id_used": None,
        "response_id": None,
        "resets_when": "python_process_restarts",
        "dry_run": True,
    }


async def send_callback(callback_url: str, result: BridgeResult) -> dict[str, Any]:
    payload = result.model_dump()
    async with httpx.AsyncClient(timeout=settings.callback_timeout_seconds) as client:
        response = await client.post(callback_url, json=payload)
    return {"status_code": response.status_code, "ok": response.is_success}


async def run_bridge_flow(signal: WatchSignal) -> BridgeResult:
    started = time.monotonic()
    captured_at = datetime.now(timezone.utc).isoformat()
    prompt = signal.prompt or settings.default_prompt
    request_log = create_request_log(captured_at)
    save_request_prompt(request_log, prompt, signal)

    try:
        async with capture_lock:
            capture_result = await asyncio.to_thread(
                capture_jpeg_frame_result,
                signal.capture_device_index,
            )

        if request_log is not None:
            capture_result.metadata["request_log"] = {
                "request_id": request_log.request_id,
                "path": str(request_log.path),
            }

        save_capture_artifacts(request_log, capture_result)
        jpeg_bytes = capture_result.jpeg_bytes
        session_metadata: dict[str, Any]

        if signal.dry_run:
            reset_note = ""
            stale_watchdog = capture_result.metadata.get("stale_watchdog", {})
            if stale_watchdog.get("recaptured_after_reactivation"):
                reset_note = " Reactivated video input before recapturing."
            text = f"Dry run captured {len(jpeg_bytes):,} bytes from the capture card.{reset_note}"
            session_metadata = dry_run_openai_session_metadata()
        else:
            image_data_url = jpeg_to_data_url(jpeg_bytes)
            openai_result = await asyncio.to_thread(ask_openai, prompt, image_data_url)
            text = openai_result.text
            if not text:
                text = "The model returned an empty response."
            session_metadata = openai_session_metadata(openai_result)

        result = BridgeResult(
            ok=True,
            text=text,
            model=settings.openai_model,
            captured_at=captured_at,
            latency_ms=round((time.monotonic() - started) * 1000),
            openai_session=session_metadata,
            capture=capture_result.metadata,
            callback=None,
        )

        callback_url = str(signal.callback_url or settings.watch_callback_url or "")
        if callback_url:
            try:
                result.callback = await send_callback(callback_url, result)
            except Exception as exc:  # noqa: BLE001 - callback failure should not hide text.
                logger.exception("Callback delivery failed")
                result.callback = {"ok": False, "error": str(exc)}

        save_response_artifacts(request_log, text, result)

        global last_result
        last_result = result
        return result
    except Exception as exc:
        save_error_artifacts(request_log, exc)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        if capture_manager is not None:
            capture_manager.deactivate()


app = FastAPI(
    title="Watch Presentation Bridge",
    summary="Capture a presentation feed and return an OpenAI response to a watch client.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    manager_active = (
        capture_manager is not None
        and getattr(capture_manager, "cap", None) is not None
        and bool(capture_manager.cap.isOpened())
    )
    with openai_session_lock:
        session_turn_count = openai_session_state.turn_count
        session_active = openai_session_state.previous_response_id is not None
    return {
        "ok": True,
        "model": settings.openai_model,
        "capture_device_index": settings.capture_device_index,
        "capture_device_names": capture_device_name_preferences(),
        "capture_backend": settings.capture_backend,
        "capture_active": manager_active,
        "openai_session_active": session_active,
        "openai_session_turn_count": session_turn_count,
        "token_required": bool(settings.bridge_token),
    }


@app.post("/watch/signal", response_model=BridgeResult)
async def watch_signal(
    signal: WatchSignal,
    _: None = Depends(require_token),
) -> BridgeResult:
    return await run_bridge_flow(signal)


@app.get("/watch/trigger", response_class=Response)
async def watch_trigger(
    prompt: str | None = Query(default=None),
    dry_run: bool = Query(default=False),
    capture_device_index: int | None = Query(default=None),
    _: None = Depends(require_token),
) -> Response:
    signal = WatchSignal(
        prompt=prompt,
        dry_run=dry_run,
        capture_device_index=capture_device_index,
    )
    result = await run_bridge_flow(signal)
    return Response(content=result.text, media_type="text/plain; charset=utf-8")


@app.get("/watch/last", response_model=BridgeResult | None)
async def watch_last(_: None = Depends(require_token)) -> BridgeResult | None:
    return last_result


@app.get("/capture/devices")
async def capture_devices(_: None = Depends(require_token)) -> dict[str, Any]:
    manager = get_capture_manager()
    selection = manager.resolve_device(None)
    return {
        "preferences": capture_device_name_preferences(),
        "selected": selection.metadata,
        "available_devices": enumerate_named_capture_devices(),
        "fallback_index": settings.capture_device_index,
    }


@app.post("/capture/reactivate")
async def capture_reactivate(
    capture_device_index: int | None = Query(default=None),
    _: None = Depends(require_token),
) -> dict[str, Any]:
    async with capture_lock:
        return await asyncio.to_thread(
            deactivate_and_reactivate_video_input,
            capture_device_index,
        )


def default_cropped_capture_path(path: Path) -> Path:
    suffix = path.suffix or ".jpg"
    return path.with_name(f"{path.stem}_cropped{suffix}")


def save_capture_test(
    path: Path,
    device_index: int | None,
    cropped_path: Path | None = None,
) -> None:
    capture_result = capture_jpeg_frame_result(device_index)
    cropped_path = cropped_path or default_cropped_capture_path(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    cropped_path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(capture_result.raw_jpeg_bytes)
    cropped_path.write_bytes(capture_result.jpeg_bytes)

    image_metadata = capture_result.metadata.get("image", {})
    print(f"Saved raw capture test frame to {path}")
    print(f"Saved env-cropped capture test frame to {cropped_path}")
    print(f"Image transform metadata: {json.dumps(image_metadata, sort_keys=True)}")


def save_capture_worker_output(output_dir: Path, device_index: int | None) -> None:
    os.environ["WATCH_BRIDGE_CAPTURE_WORKER"] = "1"
    output_dir.mkdir(parents=True, exist_ok=True)
    capture_result = capture_jpeg_frame_result(device_index)
    (output_dir / "raw_capture.jpg").write_bytes(capture_result.raw_jpeg_bytes)
    (output_dir / "sent_to_openai.jpg").write_bytes(capture_result.jpeg_bytes)
    write_json(output_dir / "capture_metadata.json", capture_result.metadata)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Watch presentation bridge.")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the HTTP bridge server")
    serve_parser.add_argument("--host", default=settings.host)
    serve_parser.add_argument("--port", default=settings.port, type=int)
    serve_parser.add_argument("--reload", action="store_true")

    capture_parser = subparsers.add_parser(
        "capture-test",
        help="Save one capture-card frame to a JPEG file",
    )
    capture_parser.add_argument("--output", default="capture_test.jpg")
    capture_parser.add_argument(
        "--cropped-output",
        default=None,
        help="Path for the env-cropped/processed test image. Defaults to OUTPUT with _cropped before the extension.",
    )
    capture_parser.add_argument("--device-index", type=int, default=None)

    worker_parser = subparsers.add_parser(
        "capture-worker",
        help=argparse.SUPPRESS,
    )
    worker_parser.add_argument("--output-dir", required=True)
    worker_parser.add_argument("--device-index", type=int, default=None)

    args = parser.parse_args()
    command = args.command or "serve"

    if command == "capture-test":
        cropped_output = Path(args.cropped_output) if args.cropped_output else None
        save_capture_test(Path(args.output), args.device_index, cropped_output)
        return

    if command == "capture-worker":
        save_capture_worker_output(Path(args.output_dir), args.device_index)
        return

    uvicorn.run(
        "watch_presentation_bridge:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
