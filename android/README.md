# Scoopz for Android

Native Kotlin (Jetpack Compose) replacement for the Termux workflow. Share a link, it downloads, it gets captioned — all inside one app.

## Flow

1. **Share** a TikTok link to *Scoopz* from any app (or tap **+** to paste from the clipboard).
2. `ShareReceiverActivity` parses the link, stores it in a Room queue and closes instantly.
3. `DownloadWorker` (WorkManager, foreground notification) resolves the link via tikwm and streams the mp4 into `Movies/Scoopz` through MediaStore. Max 3 downloads in parallel, duplicates are skipped by video id.
4. `CaptionWorker` picks the 1-2 most informative frames on-device (sharpness, faces, on-screen text via ML Kit), sends them with the TikTok title, uploader and on-screen text to NVIDIA's free OpenAI-compatible endpoint, and saves the caption plus hashtags. Up to 3 captions run at once, rate-limited, with a backup model if the main one is slow.
5. Library screen: tap cards to select (max 3), **Caption**, **Copy**, retry failures.

## Memory management

- Videos are streamed to disk in 64 KB chunks, never loaded into RAM.
- Frame extraction decodes one frame at a time and recycles every bitmap immediately.
- Coil memory cache capped at 12% of app memory, disk thumbnail cache capped at 64 MB.
- Download and caption concurrency are bounded by semaphores; caption requests are spaced by a rate limiter.

## NVIDIA setup

1. Create a free key at <https://build.nvidia.com> (Settings → API Keys).
2. Open the app → Settings → paste the key.
3. Default model: `meta/llama-3.2-11b-vision-instruct`. If you get HTTP 403, open that model's page on build.nvidia.com and use *Try API* once to enable it for your key.

The free tier is about 40 requests/minute per key; the app defaults to 30. Free vision models take one image per request, hence the stitched frame strip. No free speech-to-text model was available, so transcription is not implemented yet.

## Build

- **Android Studio:** open the `android/` folder and run. If it asks for a Gradle wrapper, accept the offered sync (or run `gradle wrapper --gradle-version 8.9`).
- **CI:** `.github/workflows/android.yml` builds a debug APK on every push to `android/`; download it from the workflow run's artifacts.

Requires Android 10+ (API 29).

## Not ported yet from the Python scripts

- Audio transcription (no free NVIDIA speech-to-text model; captions use frames only, with the script's no-speech weighting)

Ported: the caption prompt, POV rotation, style-mode rotation, caption cleanup, and the hashtag recipe (4 mandatory + up to 3 video-specific tags).
- Thumbnail pruning settings (replaced by Coil's bounded cache)
