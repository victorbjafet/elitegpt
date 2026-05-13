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

Edit `.env` with your `OPENAI_API_KEY`, a private `BRIDGE_TOKEN`, and the correct capture device settings. On macOS, prefer `CAPTURE_DEVICE_NAMES=USB3 Video,USB2 Video`; `CAPTURE_DEVICE_INDEX` is only the fallback.

## Test The Capture Card

```bash
python watch_presentation_bridge.py capture-test --output capture_test.jpg
```

This writes the raw capture-card frame to `capture_test.jpg`. It also reads the crop/resize settings from `.env` and writes the post-crop image to `capture_test_cropped.jpg`, which matches the image that would be sent to OpenAI.

If the saved image is not the capture-card feed, check `CAPTURE_DEVICE_NAMES` first. On macOS, the bridge looks for those AVFoundation names before using `CAPTURE_DEVICE_INDEX`, so `USB3 Video` and `USB2 Video` can swap indexes without breaking the script. You can still force an index with `--device-index 1`, `2`, etc. On macOS, Python or Terminal may need Camera permission for OpenCV to read the capture card. On Windows, the default `CAPTURE_BACKEND=auto` uses DirectShow because that most closely matches OBS's Video Capture Device path; switch to `CAPTURE_BACKEND=msmf` if a device behaves better through Media Foundation.

To inspect the current macOS camera list and selected index without opening the capture card:

```bash
curl -H "Authorization: Bearer YOUR_BRIDGE_TOKEN" http://127.0.0.1:8787/capture/devices
```

## Run

```bash
python watch_presentation_bridge.py serve
```

Open `http://localhost:8787/docs` for the interactive API page.

## Capture Card Watchdog

The bridge keeps the capture-card input active between requests, like an OBS source that is currently active. If the capture-card output is effectively unchanged for `STALE_FRAME_SECONDS` seconds, it calls `deactivate_and_reactivate_video_input()`.

That reset mirrors the OBS workflow: release the active device handle, pause briefly, reopen it with the configured backend, reapply width/height/FPS/format settings, warm up the input, and recapture once before sending the image to OpenAI. This is stronger than simply opening a new one-off capture because it actually tears down the held video input.

On macOS, unplug/replug can leave the old AVFoundation/OpenCV handle open but not delivering frames. If a read times out, the bridge now fully releases the handle, re-resolves `CAPTURE_DEVICE_NAMES`, retries several times, and finally tries a one-shot fresh Python subprocess capture. That fallback mimics restarting just the capture stack while keeping the web server alive.

Default settings:

```env
CAPTURE_BACKEND=auto
CAPTURE_WIDTH=1920
CAPTURE_HEIGHT=1080
CAPTURE_FPS=0
CAPTURE_FOURCC=
CAPTURE_BUFFER_SIZE=1
CAPTURE_STRICT_MODE=false
CAPTURE_READ_RETRIES=3
CAPTURE_READ_RETRY_PAUSE_SECONDS=1.5
CAPTURE_SUBPROCESS_FALLBACK_ENABLED=true
CAPTURE_SUBPROCESS_TIMEOUT_SECONDS=30
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
CAPTURE_DEVICE_NAMES=USB3 Video,USB2 Video
CAPTURE_WIDTH=1920
CAPTURE_HEIGHT=1080
CAPTURE_FPS=60
CAPTURE_FOURCC=MJPG
```

Some capture cards ignore unsupported modes and fall back silently. The response metadata and `capture_metadata.json` include both `requested_mode` and the actual device properties. Set `CAPTURE_STRICT_MODE=true` if you want the request to fail when the device does not accept the requested width/height.

The bridge also checks the actual frame dimensions returned by OpenCV. This matters because some backends report the requested mode through device properties even when the delivered frame is smaller.

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

## OpenAI Session Memory

The bridge chains OpenAI Responses API calls with `previous_response_id`, so each non-dry-run request continues the same model conversation while the Python process is running. The session starts on the first OpenAI request and resets only when the bridge process restarts.

Each result and request log includes `openai_session` metadata with the response id used for chaining. Because chained context is included in later calls, longer sessions can cost more tokens over time.

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

## Cloudflare Tunnel

Cloudflare Tunnel is the recommended off-LAN path. It gives the Watch Shortcut a public HTTPS URL that routes back to this laptop over an outbound `cloudflared` connection. You do not need router port forwarding or a public IP.

Use a Quick Tunnel first, then move to a named tunnel when you want a stable URL.

### Option A: Quick Tunnel

Quick Tunnels are temporary and generate a random `trycloudflare.com` URL. They are perfect for first testing.

Install `cloudflared` on macOS:

```bash
brew install cloudflared
```

On Windows, install `cloudflared` from Cloudflare's downloads page, then run the commands below in PowerShell or Command Prompt.

Start the bridge in one terminal:

```bash
source .venv/bin/activate
python watch_presentation_bridge.py serve
```

On this Mac, you can use:

```bash
./terminal1_mac.sh
```

Start Cloudflare in a second terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

For the named tunnel used by the local helper scripts, store the token in `.env` as `CLOUDFLARED_TUNNEL_TOKEN`, then run:

```bash
./terminal2_mac.sh
```

Copy the generated URL. It will look like:

```text
https://example-random-name.trycloudflare.com
```

Your Watch trigger URL is:

```text
https://example-random-name.trycloudflare.com/watch/trigger
```

Test it from any browser or terminal:

```bash
curl -H "Authorization: Bearer YOUR_BRIDGE_TOKEN" \
  "https://example-random-name.trycloudflare.com/health"
```

### Option B: Named Tunnel

Use this when you want a stable URL such as:

```text
https://presentation.yourdomain.com/watch/trigger
```

Requirements:

- A Cloudflare account.
- A domain added to Cloudflare.
- `cloudflared` installed on the laptop that runs this bridge.

Dashboard setup:

1. Open the Cloudflare dashboard.
2. Go to `Zero Trust` or `Networking` > `Tunnels`.
3. Select `Create Tunnel`.
4. Name it, for example `presentation-bridge`.
5. Choose your operating system and copy Cloudflare's install/run command.
6. Run that command on this laptop.
7. Wait until the tunnel shows `Healthy`.
8. Add a published application route.
9. Public hostname: `presentation.yourdomain.com`.
10. Service URL: `http://localhost:8787`.
11. Save the route.

Then your Watch trigger URL is:

```text
https://presentation.yourdomain.com/watch/trigger
```

Keep the Python bridge running whenever you want the Watch shortcut to work:

```bash
python watch_presentation_bridge.py serve
```

If you installed `cloudflared` as a service, the tunnel can run in the background. If you are using a Quick Tunnel, keep the `cloudflared tunnel --url ...` terminal open.

## Apple Watch Shortcut Flow

Create this on the iPhone paired with your Apple Watch:

1. Open `Shortcuts`.
2. Tap `+`.
3. Name it `Presentation Assist`.
4. Add action: `URL`.
5. Set the URL to your Cloudflare trigger URL:

   ```text
   https://YOUR-CLOUDFLARE-HOSTNAME/watch/trigger
   ```

6. Add action: `Get Contents of URL`.
7. Open `Show More`.
8. Set method to `GET`.
9. Add header:

   ```text
   Authorization: Bearer YOUR_BRIDGE_TOKEN
   ```

10. Add action: `Show Result`.
11. Use the output from `Get Contents of URL`.
12. Open the shortcut details and turn on `Show on Apple Watch`.
13. On Apple Watch, open the `Shortcuts` app and run `Presentation Assist`.

You can override the prompt from the URL:

```text
https://YOUR-CLOUDFLARE-HOSTNAME/watch/trigger?prompt=Summarize%20the%20slide%20in%202%20bullets
```

Keep using the `Authorization` header even when you add query parameters.

For a native app or richer Shortcut, POST JSON to `/watch/signal`:

```json
{
  "prompt": "Summarize the current presentation slide.",
  "dry_run": false
}
```

Send the token as either `Authorization: Bearer YOUR_BRIDGE_TOKEN`, `X-Bridge-Token: YOUR_BRIDGE_TOKEN`, or the `token` query parameter.

## Endpoints

- `GET /health`: basic status.
- `GET /watch/trigger`: capture and return plain text, best for a simple Watch Shortcut.
- `POST /watch/signal`: capture and return JSON with text, latency, and callback status.
- `GET /watch/last`: return the most recent JSON result.
- `GET /capture/devices`: list name-resolved capture devices and the selected index.
- `POST /capture/reactivate`: manually release/reopen the capture-card input, re-resolving `CAPTURE_DEVICE_NAMES`.

## Notes

- This framework does not create a native Apple Watch app. It gives you the service contract that Watch Shortcuts or an app can trigger.
- If `WATCH_CALLBACK_URL` is set, each result is POSTed there after generation.
- The default model is configurable with `OPENAI_MODEL`.
