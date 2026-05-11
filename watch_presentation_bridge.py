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
    capture: dict[str, Any] | None = None
    callback: dict[str, Any] | None = None


capture_lock = asyncio.Lock()
capture_watchdog_state = CaptureWatchdogState()
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

        return {
            "ok": True,
            "was_active": was_active,
            "device_index": previous_index,
            "backend": previous_backend or None,
        }

    def reactivate(self, index: int) -> dict[str, Any]:
        started = time.monotonic()
        attempts = max(1, settings.video_reactivation_attempts)
        pause = max(0.0, settings.video_reactivation_pause_seconds)
        last_error = ""
        deactivation = self.deactivate()

        for attempt in range(1, attempts + 1):
            logger.warning(
                "Reactivating capture device index %s, attempt %s/%s",
                index,
                attempt,
                attempts,
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
                "activation": activation,
            }

        return {
            "ok": False,
            "device_index": index,
            "attempts": attempts,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "deactivation": deactivation,
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

    def read_frame(self, index: int) -> tuple[Any, dict[str, Any]]:
        activation = self.activate(index)
        frame = self._read_frame_from_active()

        read_failure_reactivation = None
        if frame is None:
            read_failure_reactivation = self.reactivate(index)
            if read_failure_reactivation.get("ok"):
                frame = self._read_frame_from_active()

        if frame is None:
            raise RuntimeError(f"Timed out waiting for a frame from device index {index}")

        height, width = frame.shape[:2]
        metadata = {
            "device_index": index,
            "backend": self.backend_name,
            "activation": activation,
            "read_failure_reactivation": read_failure_reactivation,
            "requested_mode": self.requested_properties(),
            "raw_frame": {"width": width, "height": height},
        }
        return frame, metadata

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
    index = settings.capture_device_index if device_index is None else device_index
    return get_capture_manager().reactivate(index)


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


def capture_jpeg_frame_result(device_index: int | None = None) -> CaptureResult:
    index = settings.capture_device_index if device_index is None else device_index
    manager = get_capture_manager()
    cv2 = manager.cv2
    frame, read_metadata = manager.read_frame(index)
    stale_watchdog = evaluate_frame_staleness(cv2, frame, index)

    if stale_watchdog.get("reactivation_needed"):
        reset_result = manager.reactivate(index)
        stale_watchdog["reactivation"] = reset_result
        if reset_result.get("ok"):
            frame, read_metadata = manager.read_frame(index)
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


def ask_openai(prompt: str, image_data_url: str) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        max_output_tokens=settings.max_output_tokens,
        input=[
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
    )

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

        if signal.dry_run:
            reset_note = ""
            stale_watchdog = capture_result.metadata.get("stale_watchdog", {})
            if stale_watchdog.get("recaptured_after_reactivation"):
                reset_note = " Reactivated video input before recapturing."
            text = f"Dry run captured {len(jpeg_bytes):,} bytes from the capture card.{reset_note}"
        else:
            image_data_url = jpeg_to_data_url(jpeg_bytes)
            text = await asyncio.to_thread(ask_openai, prompt, image_data_url)
            if not text:
                text = "The model returned an empty response."

        result = BridgeResult(
            ok=True,
            text=text,
            model=settings.openai_model,
            captured_at=captured_at,
            latency_ms=round((time.monotonic() - started) * 1000),
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
    return {
        "ok": True,
        "model": settings.openai_model,
        "capture_device_index": settings.capture_device_index,
        "capture_backend": settings.capture_backend,
        "capture_active": manager_active,
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


def save_capture_test(path: Path, device_index: int | None) -> None:
    jpeg = capture_jpeg_frame(device_index)
    path.write_bytes(jpeg)
    print(f"Saved capture test frame to {path}")


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
    capture_parser.add_argument("--device-index", type=int, default=None)

    args = parser.parse_args()
    command = args.command or "serve"

    if command == "capture-test":
        save_capture_test(Path(args.output), args.device_index)
        return

    uvicorn.run(
        "watch_presentation_bridge:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
