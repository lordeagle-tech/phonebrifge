# Laptop-Phone Bridge

Control your Parrot OS laptop from your Android phone — no app install needed. Just open your phone's browser.

## Features

- **File Manager** — Browse, download, and upload files between your laptop and phone
- **Web Terminal** — Run commands on your laptop directly from your phone's browser
- **System Stats** — Live CPU, RAM, disk, battery, and network monitoring
- **Screen Mirror** — View your laptop's screen live on your phone browser
- **Phone Mirror (scrcpy)** — Full interactive phone screen on your laptop — touch, keyboard, and record via `/mirror-setup`
- **Laptop-to-Laptop Peer Control** — Control other paired laptops remotely from your browser via `/peer-control`
- **Voice Calls** — Place a peer-to-peer audio call from your phone to whoever's near the laptop, with mute and call timer
- **Remote Touchpad** — Move the mouse, click, and scroll from your phone
- **Remote Keyboard** — Type and send keystrokes to your laptop from your phone
- **Clipboard Sync** — Copy from laptop to phone and push text from phone to laptop
- **Activity Log** — See every recent login, file transfer, and clipboard touch from the phone UI
- **PIN Protection + Lockout** — Auto-locks the IP after 5 wrong PINs for 15 minutes
- **HTTPS by default** — Self-signed certificate auto-generated on first run
- **Inactivity Timeout** — Auto-logs you out after 30 min idle (configurable)

---

## Requirements

- Python 3.9 or newer
- Parrot OS (or any Linux)
- Both laptop and phone on the same Wi-Fi network

---

## Setup

### 1. Install dependencies

```bash
cd phone-bridge
pip install -r requirements.txt
```

### 2. Start the server

```bash
python run.py
```

You'll see output like:

```
====================================================
  📱  Laptop-Phone Bridge
====================================================

  Server URL : http://192.168.1.42:8765
  Access PIN : 847291

  Scan the QR code with your phone:

  [QR CODE]

  Make sure your phone is on the same Wi-Fi network.
  Press Ctrl+C to stop the server.
====================================================
```

### 3. Connect from your phone

1. Make sure your phone is on the same Wi-Fi as your laptop
2. Scan the QR code, or manually type the URL into your phone's browser
3. Enter the 6-digit PIN shown in the terminal
4. You're in!

---

## Options

```bash
python run.py --help
```

| Flag | Default | Description |
|------|---------|-------------|
| `--pin` | auto | Set a custom PIN (min 4 digits) |
| `--port` | 8765 | Port to listen on |
| `--host` | 0.0.0.0 | Host to bind to |
| `--remote` | off | Expose the server through a secure public tunnel so you can connect from any network (work, cafe, mobile data) |
| `--remote-domain HOST` | — | Bind the ngrok tunnel to a reserved domain so the URL stays the same across restarts (implies `--remote`). See [Keep the same URL across restarts](#keep-the-same-url-across-restarts-recommended). |
| `--no-tls` | off | Disable HTTPS and serve plain HTTP (debugging only — not recommended) |
| `--session-timeout` | 30 | Auto-logout after N minutes of inactivity (0 disables) |
| `--stun URL` | Google STUN | STUN server for WebRTC (repeatable). Default: `stun:stun.l.google.com:19302` |
| `--no-stun` | off | Disable STUN entirely (LAN-only WebRTC; fully offline-friendly) |

**Example with custom PIN:**
```bash
python run.py --pin 1234
```

---

## Voice calls (phone ↔ laptop)

Call the laptop straight from your phone — no extra app, no extra port. The audio is peer-to-peer over WebRTC and the laptop's `/viewer.html` page rings until someone picks up.

**Phone → Laptop**
1. On your **laptop**, open `https://<laptop-ip>:8765/viewer.html` in any browser and sign in with the same PIN. The viewer keeps a quiet listener open in the background so it can ring at any time.
2. On your **phone**, open the **Stats** tab and tap the big **📞 Call laptop** button. Your browser will ask for microphone permission the first time.
3. The laptop viewer rings (visual banner + soft tone). Click **Accept** to connect or **Decline** to send the call away.
4. While in a call, both ends show a timer. Either side can tap **Mute** or **End call** at any time.

**Laptop → Phone**
1. Make sure your **phone** is signed in (the dashboard / Stats tab is open). It quietly listens for incoming calls in the background.
2. On the **laptop** viewer, tap the **📞 Call phone** button in the top bar.
3. The phone shows the same Accept / Decline ringer; once accepted both ends share the same in-call controls.

Tips:
- Echo cancellation, noise suppression, and auto-gain are all enabled by default for clearer audio.
- Calls happen on a separate signaling room ("call") and a separate peer connection from the screen-share feature, so you can talk while sharing your screen.
- If the laptop doesn't ring, make sure `/viewer.html` is open and signed in — the phone needs the viewer to be online to deliver the call.
- Microphone access on the phone normally requires HTTPS, which the bridge serves by default. If you turned HTTPS off with `--no-tls`, browsers will refuse mic access.
- Tested in recent Chrome/Edge/Firefox on desktop and Chrome/Firefox on Android. Safari (iOS/macOS) also works once you tap to accept the call (Safari blocks autoplay of the ringing tone until the page has been clicked).

---

## One-click full phone mirror (scrcpy)

A browser page can't capture other apps on your phone or pass touch input to Android — that's a hard browser sandbox limit. The classic `scrcpy` tool already mirrors the full Android screen with mouse + keyboard control, so the bridge bundles a one-click launcher around it.

Open this page on the **laptop** (not the phone) and sign in with the same PIN:

```
http://<laptop-ip>:8765/mirror-setup
```

The page walks you through four steps:

1. **Dependencies** — shows whether `scrcpy` and `adb` are installed; if not, gives you a one-click copy of `sudo apt install scrcpy adb` for Parrot/Debian/Ubuntu.
2. **Connected devices** — runs `adb devices -l` and lists everything plugged in over USB or already paired over Wi-Fi, with friendly model names and a status pill (`ready`, `unauthorized`, `offline`).
3. **Pair wirelessly** (one-time per phone). Two options:
   - **QR code (Android 11+, recommended)** — click *Show QR code*, then on your phone open *Wireless debugging → Pair device with QR code* and point your camera at the laptop screen. The bridge auto-detects the phone over mDNS, pairs, and connects with no typing.
   - **Manual** — expand *Or pair manually with a 6-digit code* and use *Wireless debugging → Pair device with pairing code*. Type the IP, port, and 6-digit code into the form, then enter the regular Wireless-debugging IP+port and tap *Connect*.
4. **Launch the mirror** — pick a device, choose a preset (default / low-bitrate / record-to-file), and click *Launch mirror*. A native scrcpy window opens on your laptop desktop. The page shows live status and the latest scrcpy log lines, and the *Stop* button kills the process cleanly.

Recently used devices are remembered under `~/.phone-bridge/scrcpy.json`, so when you come home each device is one *Reconnect &amp; launch* tap away.

### Pop up the mirror from the terminal

If the bridge is already running, you can open the native scrcpy window straight from a terminal — handy when you want to drive the phone with your mouse and scroll wheel without touching the browser:

```bash
python run.py mirror             # picks the only paired phone, or prompts
python run.py mirror <serial>    # skip the picker
python run.py mirror --quality high
python run.py mirror --record    # save session to ~/.phone-bridge/recordings/
```

The CLI authenticates against the running bridge using a short-lived token the server writes to `~/.phone-bridge/cli_token` (mode 600, loopback only) — no PIN re-entry needed. The token file is removed when you stop the bridge.

Tips:
- Wireless debugging on Android requires Developer Options to be enabled (tap *Build number* 7 times in *About phone*).
- If a USB device shows `unauthorized`, accept the *Allow USB debugging* prompt that pops up on the phone.
- Recording goes to `~/.phone-bridge/recordings/phone-<timestamp>.mp4`.
- Stopping the bridge with Ctrl+C also kills any scrcpy launched from the page.

---

## Linking other laptops (peers)

If you have more than one laptop you want to reach the same way (extra
desks, a second machine in another room), you can link them as
**peers** of your main bridge. The peers register themselves with the
main laptop and show up as "online" in `GET /api/peers`. Once linked
they become targets for upcoming features (remote-screen view from
the main laptop, phone machine picker — both are queued as separate
work items).

### One-time pairing

1. On your **main laptop**, start the bridge as usual (`python run.py`).
   The startup banner now shows a 6-digit **Peer pair code** that
   rotates every 5 minutes:
   ```
   🔗 Peer pair code : 482917  (rotates in 4m 47s)
      Run on another laptop:
        python run.py peer --main https://192.168.1.42:8765 --pair-code 482917
   ```

2. On the **secondary laptop** (after `pip install -r requirements.txt`),
   run the printed command. The peer agent pairs with the main bridge,
   stores its long-lived secret in `~/.phone-bridge/peers.json`
   (mode 600) and starts heartbeating every 5 seconds.

3. From then on, just running `python run.py peer` (no flags) on that
   secondary laptop will reuse the saved link — no code re-entry. If
   the main bridge ever changes its LAN IP, pass the new URL with
   `--main` once and the peer will keep using its existing secret.

### Re-pairing or forgetting a peer

- Run `python run.py peer --re-pair --main <url> --pair-code <code>`
  on the secondary laptop to discard the saved secret and pair afresh
  (useful if you revoke the peer or repoint at a different main).
- On the main laptop, `DELETE /api/peers/<peer_id>` (PIN-auth) revokes
  a peer's secret. Its WebSocket drops on the next heartbeat.

### Remote-controlling another laptop

Once a peer has paired, you can take over its screen and keyboard from
your main laptop's browser:

1. On the **main laptop**, open
   `https://<main-laptop-ip>:8765/peer-control` and sign in with the
   same PIN you use for the phone.
2. Pick the peer you want from the dropdown at the top — the
   selector greys out peers that are offline or paired with an older
   agent that doesn't expose the screen yet.
3. The peer's desktop shows up in the page. Click anywhere to move
   the peer's mouse there, right-click for a context menu, scroll
   with your mouse wheel, and just type — every keystroke goes to the
   peer's currently-focused window. The status bar shows the live
   FPS and round-trip latency over the input channel.
4. Hit **Disconnect** in the top-right when you're done. If the peer
   drops mid-session, the page freezes the last frame and shows a
   "Peer disconnected — reconnecting…" overlay; control resumes
   automatically once the peer comes back.

You can open as many tabs as you like to control different peers in
parallel.

By default the peer agent listens on port `8766` for the screen +
input traffic from the main bridge (override with `--peer-port`).
Open that port on the peer's firewall — only the main laptop ever
hits it, and every request must carry the per-peer secret, but the
port still needs to be reachable on the LAN.

### Targeting a peer from your phone

Once a peer is paired, your phone can point individual tabs at the
peer instead of the main laptop — handy when you're sitting next to
the main bridge but want to copy a file off the laptop in your
study, or peek at its CPU.

A pill at the top-right of the phone UI shows the active tab's
current target — either **→ this laptop** (the bridge you signed
into) or **→ &lt;hostname&gt;** (a peer). Tap the pill to open a
sheet listing every linked machine; pick one and the tab's next
fetch retargets there.

The picker is per-tab, so you can browse files on your study laptop
while the Stats tab still graphs the main bridge. The targetable
tabs are **Files, Stats, Terminal, Screen Mirror, and Control**
(touchpad + keyboard + clipboard share a single target on the
Control page so retargeting once swaps every interaction over).
Tabs without a peer-side equivalent (Share, Activity, Photos,
SMS, Notifications, Ring) hide the pill.

The Screen Mirror tab transparently switches transports based on
target: the main laptop streams binary JPEG frames over a
WebSocket, while peers serve the same frames as MJPEG over plain
HTTP — the phone UI handles both, and the FPS / quality sliders
work identically in either mode.

The peer's per-peer secret never leaves the main bridge — your
phone just hits `/api/peers/<id>/…` on the main bridge, which
re-signs the request with the secret it holds in its registry and
forwards it to the peer agent on port 8766.

Offline peers (or peers paired with an older agent that doesn't
expose the file/terminal endpoints) are greyed out in the picker
so you can't accidentally retarget to a dead machine. If the peer
a tab is **already** pointed at goes offline mid-session, the
phone shows a one-shot toast — *"&lt;name&gt; went offline — tap the
menu to switch machine"* — and the Stats tab pauses its 3-second
polling loop until you pick a reachable machine, so the screen
isn't covered in 503s while you decide what to do.

### Notes

- Peers authenticate with their per-peer secret derived from the pair
  code — the main laptop's PIN is never sent to peers.
- Pair codes are LAN-friendly only; don't try to pair across the
  public ngrok tunnel (the secondary laptop won't be able to keep the
  heartbeat WS open through ngrok's free-tier limits anyway).
- The peer registry lives at `~/.phone-bridge/peers.json` on the main
  laptop (mode 600). Delete it to forget every peer at once.

---

## Remote Access (connect from anywhere)

By default the bridge only works when your phone and laptop share the same Wi-Fi. To reach your laptop from work, a cafe, or mobile data, start the bridge with `--remote`:

```bash
python run.py --remote
```

This opens a secure HTTPS tunnel (via [ngrok](https://ngrok.com)) and prints a public URL plus QR code that works from any network. The PIN still protects the session.

```
  Server URL : https://abc123.ngrok-free.app
  Access PIN : 847291
  Mode       : REMOTE (secure public tunnel)
```

### One-time ngrok setup

ngrok requires a free auth token:

1. Sign up at <https://dashboard.ngrok.com/signup>
2. Copy your token from <https://dashboard.ngrok.com/get-started/your-authtoken>
3. Export it before starting the bridge:

```bash
export NGROK_AUTHTOKEN=<your-token>
python run.py --remote
```

The `pyngrok` package (installed via `requirements.txt`) downloads the ngrok binary automatically on first use.

### Keep the same URL across restarts (recommended)

Free ngrok tunnels get a brand new public URL every time the bridge restarts, which means the saved bookmark and QR code on your phone stop working. ngrok's free plan now lets you reserve **one static domain** per account — use it so your phone can keep the bookmark forever.

1. Open <https://dashboard.ngrok.com/cloud-edge/domains> and click **New Domain**. ngrok hands you a free domain like `friendly-otter-12.ngrok-free.app`.
2. Start the bridge with `--remote-domain` pointing at that name (it implies `--remote`):

```bash
export NGROK_AUTHTOKEN=<your-token>
python run.py --remote-domain friendly-otter-12.ngrok-free.app
```

The banner will show the same URL every time, so you can bookmark it on your phone once and forget about it. You can pass either a bare hostname or a full `https://…` URL.

---

## Security Notes

- **HTTPS by default**: The bridge generates a self-signed cert under `~/.phone-bridge/` on first run. Browsers will show a warning the first time you connect — tap **Advanced → Proceed**. The startup banner prints the cert's SHA-256 fingerprint so you can verify the connection is genuine.
- **PIN lockout**: After 5 wrong PINs from the same IP, that IP is locked out for 15 minutes. Lockouts and every login attempt are recorded in the audit log.
- **Inactivity timeout**: Sessions auto-logout after 30 min idle (override with `--session-timeout 60` or set `0` to disable). Sessions also expire after 24 hours and when the server restarts.
- **Rate limiting**: All API endpoints are rate-limited per IP (auth: 10/min, files & clipboard: 60/min, others: 120/min) to slow brute-force and abuse.
- **Audit log**: Every login, file transfer, clipboard touch, lockout, and rate-limit hit is appended as JSON-lines to `~/.phone-bridge/audit.log` (rotated at 1 MB, 3 backups kept). The phone UI's **Activity** tab shows the most recent 50 events.
- For local-only use (default), don't port-forward in your router.
- For remote use (`--remote`), the ngrok tunnel is HTTPS-encrypted and the PIN gates every session — but anyone with the URL **and** the PIN can connect, so keep both private. Use a strong custom PIN (`--pin`).
- Stop the server (Ctrl+C) when you're done; the tunnel closes with it.

### Resetting security state

To force a new HTTPS certificate, clear the audit log, or unlock all locked-out IPs (lockout state lives only in memory and clears on restart):

```bash
rm ~/.phone-bridge/cert.pem ~/.phone-bridge/key.pem   # new cert on next start
rm ~/.phone-bridge/audit.log*                          # clear audit history
```

---

## Troubleshooting

**Phone can't reach the server:**
- Make sure both devices are on the same Wi-Fi network (or use `--remote` for any network)
- Check your laptop's firewall: `sudo ufw allow 8765/tcp`
- Try the URL manually in the browser if QR scan fails

**Remote tunnel (`--remote`) fails to start:**
- Make sure `NGROK_AUTHTOKEN` is set (see Remote Access section)
- Free ngrok accounts allow one tunnel at a time — kill any old `ngrok` process: `pkill ngrok`
- Check your internet connection on the laptop

**Terminal not connecting:**
- Tap "Reconnect" in the terminal toolbar
- Make sure `bash` is installed on your laptop

**Permission denied on files:**
- The file manager only shows your home directory and its contents
- System files outside your home directory are not accessible (by design)

**Screen mirror not working:**
- Requires a running graphical session (X11 or Wayland with XWayland)
- Make sure the `mss` and `Pillow` packages are installed (`pip install mss Pillow`)
- Try lowering the FPS (5-10) and quality (50-60) for better performance on slow networks

**Remote control (touchpad/keyboard) not working:**
- Requires X11 display access; make sure you're running in a graphical session
- On Wayland: install `xdotool` as a fallback if pynput doesn't detect the display
- Try the "Reconnect" button in the Control tab if the connection drops
- `pynput` may require the package `python3-xlib` on some systems: `sudo apt install python3-xlib`

**Clipboard sync not working:**
- Requires `xclip` or `xsel` installed: `sudo apt install xclip`
- Run `pyperclip` to verify it can access your clipboard: `python3 -c "import pyperclip; print(pyperclip.paste())"`
