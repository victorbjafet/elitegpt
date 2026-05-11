# Watch Presentation Bridge

Small Python framework for:

1. receiving an Apple Watch signal over HTTP,
2. grabbing a frame from a capture card,
3. sending that image plus a prompt to OpenAI,
4. returning the response text for the Watch to display.

The Apple Watch part is intentionally just HTTP. The easiest first version is a Watch Shortcut that calls this service with "Get Contents of URL" and then "Show Result". Later, a native Watch/iPhone companion app can call the same endpoints.

## Setup

This repo includes a local `.python-version` for Python 3.11.14 because that interpreter is available on this machine. Any normal Python 3.11+ install should work.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your `OPENAI_API_KEY`, a private `BRIDGE_TOKEN`, and the correct `CAPTURE_DEVICE_INDEX`.

## Test The Capture Card

```bash
python watch_presentation_bridge.py capture-test --device-index 0 --output capture_test.jpg
```

If the saved image is not the capture-card feed, retry with `--device-index 1`, `2`, etc. On macOS, Python or Terminal may need Camera permission for OpenCV to read the capture card. On Windows, the default `CAPTURE_BACKEND=auto` uses DirectShow because that most closely matches OBS's Video Capture Device path; switch to `CAPTURE_BACKEND=msmf` if a device behaves better through Media Foundation.

## Run

```bash
python watch_presentation_bridge.py serve
```

Open `http://localhost:8787/docs` for the interactive API page.

## Capture Card Watchdog

The bridge keeps the capture-card input active between requests, like an OBS source that is currently active. If the capture-card output is effectively unchanged for `STALE_FRAME_SECONDS` seconds, it calls `deactivate_and_reactivate_video_input()`.

That reset mirrors the OBS workflow: release the active device handle, pause briefly, reopen it with the configured backend, reapply width/height/FPS/format settings, warm up the input, and recapture once before sending the image to OpenAI. This is stronger than simply opening a new one-off capture because it actually tears down the held video input.

Default settings:

```env
CAPTURE_BACKEND=auto
CAPTURE_WIDTH=1920
CAPTURE_HEIGHT=1080
CAPTURE_FPS=0
CAPTURE_FOURCC=
CAPTURE_BUFFER_SIZE=1
CAPTURE_STRICT_MODE=false
CAPTURE_STALE_WATCHDOG_ENABLED=true
STALE_FRAME_SECONDS=20
STALE_FRAME_DIFF_THRESHOLD=2.0
VIDEO_REACTIVATION_ATTEMPTS=1
VIDEO_REACTIVATION_PAUSE_SECONDS=1.0
```

If a presentation slide legitimately stays static for a long time, increase `STALE_FRAME_SECONDS` or set `CAPTURE_STALE_WATCHDOG_ENABLED=false`.

You can also force a reset manually:

```bash
curl -X POST "http://localhost:8787/capture/reactivate?token=YOUR_BRIDGE_TOKEN"
```

## Capture Card Mode

`CAPTURE_WIDTH`, `CAPTURE_HEIGHT`, `CAPTURE_FPS`, and `CAPTURE_FOURCC` are the requested capture-card mode. This is the closest OpenCV equivalent to setting the resolution/FPS/video format in OBS's Video Capture Device properties. The bridge applies these settings when it activates or reactivates the device.

```env
CAPTURE_WIDTH=1920
CAPTURE_HEIGHT=1080
CAPTURE_FPS=60
CAPTURE_FOURCC=MJPG
```

Some capture cards ignore unsupported modes and fall back silently. The response metadata and `capture_metadata.json` include both `requested_mode` and the actual device properties. Set `CAPTURE_STRICT_MODE=true` if you want the request to fail when the device does not accept the requested width/height.

## Post-Capture Crop And Upload Size

Crop and upload resizing happen after the screenshot is captured. These are predefined in `.env`, not supplied by the Watch request.

Crop first:

```env
IMAGE_CROP_LEFT=0
IMAGE_CROP_TOP=0
IMAGE_CROP_WIDTH=0
IMAGE_CROP_HEIGHT=0
```

`IMAGE_CROP_WIDTH=0` or `IMAGE_CROP_HEIGHT=0` means "use the rest of the frame" from the configured left/top. For example, to crop a 1280x720 region starting 320 pixels from the left and 180 pixels from the top:

```env
IMAGE_CROP_LEFT=320
IMAGE_CROP_TOP=180
IMAGE_CROP_WIDTH=1280
IMAGE_CROP_HEIGHT=720
```

Resize after crop:

```env
IMAGE_OUTPUT_WIDTH=1280
IMAGE_OUTPUT_HEIGHT=720
JPEG_QUALITY=92
```

If only one output side is set, the other side is calculated to preserve aspect ratio. If both are set, the image is resized to that exact size.

## Debug Request Logs

Every Watch request is saved under `REQUEST_LOG_ROOT`, with one folder per request:

```env
REQUEST_LOG_ENABLED=true
REQUEST_LOG_ROOT=request_logs
```

Each request folder contains:

- `raw_capture.jpg`: the full capture-card frame before crop/resize.
- `sent_to_openai.jpg`: the exact image saved before it is sent to OpenAI.
- `prompt.txt`: the prompt used for that request.
- `response.txt`: the model response, or the dry-run response.
- `capture_metadata.json`: capture backend, requested/actual mode, crop/resize, stale-frame watchdog details.
- `request.json`, `signal.json`, `result.json`: structured request/response metadata.
- `error.txt` and `traceback.txt` if the request fails after the folder is created.

## Apple Watch Shortcut Flow

On your iPhone/Watch Shortcuts app:

1. Add "Get Contents of URL".
2. URL:
   `http://YOUR_MAC_LAN_IP:8787/watch/trigger?token=YOUR_BRIDGE_TOKEN`
3. Method: `GET`.
4. Add "Show Result" using the contents returned by the URL.

You can override the prompt from the URL:

```text
http://YOUR_MAC_LAN_IP:8787/watch/trigger?token=YOUR_BRIDGE_TOKEN&prompt=Summarize%20the%20slide%20in%202%20bullets
```

For a native app or richer Shortcut, POST JSON to `/watch/signal`:

```json
{
  "prompt": "Summarize the current presentation slide.",
  "dry_run": false
}
```

Send the token as either `Authorization: Bearer YOUR_BRIDGE_TOKEN`, `X-Bridge-Token: YOUR_BRIDGE_TOKEN`, or the `token` query parameter.

## Off-LAN Watch Flow Options

The cleanest off-LAN setup is still a Watch Shortcut calling one HTTPS URL and showing the returned text. Apple documents that Apple Watch can run enabled shortcuts, and Shortcuts can make API requests with "Get Contents of URL". So the best move is usually to keep this Python service exactly as-is, then expose it through a tunnel:

```bash
python watch_presentation_bridge.py serve
cloudflared tunnel --url http://localhost:8787
```

Then point the Watch Shortcut at:

```text
https://YOUR-TUNNEL.trycloudflare.com/watch/trigger?token=YOUR_BRIDGE_TOKEN
```

Good options:

- Cloudflare Tunnel: best default if you want a stable public hostname later. It creates outbound-only encrypted connections, so you do not need port forwarding or a public IP.
- ngrok: fastest temporary demo path. `ngrok http 8787` gives you a public HTTPS URL routed back to this laptop.
- Tailscale Funnel: good if you already use Tailscale. It exposes a local service to the broader internet, but has port/platform constraints and is beta.
- Discord relay: possible, but clunkier. The Watch Shortcut can post to a Discord incoming webhook, and a laptop bot can listen through Discord Gateway WebSockets and reply in a channel. That avoids exposing this laptop directly, but viewing the result on Apple Watch depends on Discord notifications or a third-party Watch Discord app, and a bot may need Discord's Message Content intent.

Recommendation: use Cloudflare Tunnel or ngrok for the first real test. Use Discord only if you specifically want the response to live in a Discord channel/history.

## Endpoints

- `GET /health`: basic status.
- `GET /watch/trigger`: capture and return plain text, best for a simple Watch Shortcut.
- `POST /watch/signal`: capture and return JSON with text, latency, and callback status.
- `GET /watch/last`: return the most recent JSON result.
- `POST /capture/reactivate`: manually release/reopen the capture-card input.

## Notes

- This framework does not create a native Apple Watch app. It gives you the service contract that Watch Shortcuts or an app can trigger.
- If `WATCH_CALLBACK_URL` is set, each result is POSTed there after generation.
- The default model is configurable with `OPENAI_MODEL`.
