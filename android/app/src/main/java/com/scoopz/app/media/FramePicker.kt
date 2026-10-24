package com.scoopz.app.media

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.media.MediaMetadataRetriever
import android.net.Uri
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.face.FaceDetection
import com.google.mlkit.vision.face.FaceDetector
import com.google.mlkit.vision.face.FaceDetectorOptions
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream

/**
 * Picks the 1-2 most informative frames of a video, entirely on the phone.
 *
 * It samples [SAMPLES] frames and scores each by sharpness, whether a face is
 * prominent (detection only, never identification) and how much readable text is
 * on screen (subtitles, name captions). Only the best frames are sent to the model,
 * which makes the request smaller and faster. The text found on screen is returned
 * too, so the caption can use names that appear in the video.
 *
 * Memory: one full frame is decoded at a time; at most [KEEP] small bitmaps are held.
 */
object FramePicker {
    class Picked(val jpeg: ByteArray, val onScreenText: String)

    private const val SAMPLES = 8
    private const val KEEP = 3
    private const val FRAME_HEIGHT = 384
    private const val MAX_BYTES = 140_000 // NVIDIA's free endpoints stall on large inline images

    private class Candidate(val index: Int, val bitmap: Bitmap, val score: Float, val text: String)

    suspend fun pick(context: Context, uri: Uri): Picked? = withContext(Dispatchers.IO) {
        val retriever = MediaMetadataRetriever()
        val kept = ArrayList<Candidate>(KEEP + 1)
        val faceDetector = FaceDetection.getClient(
            FaceDetectorOptions.Builder().setPerformanceMode(FaceDetectorOptions.PERFORMANCE_MODE_FAST).build()
        )
        val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
        try {
            retriever.setDataSource(context, uri)
            val durationMs = retriever
                .extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull() ?: return@withContext null

            for (i in 0 until SAMPLES) {
                val p = 0.08 + 0.84 * i / (SAMPLES - 1)
                val full = retriever.getFrameAtTime((durationMs * p * 1000).toLong(), MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
                    ?: continue
                val w = (full.width.toFloat() * FRAME_HEIGHT / full.height).toInt().coerceAtLeast(1)
                val bmp = Bitmap.createScaledBitmap(full, w, FRAME_HEIGHT, true)
                if (bmp !== full) full.recycle()

                val (frameScore, text) = rate(bmp, faceDetector, recognizer)
                kept += Candidate(i, bmp, frameScore, text)
                if (kept.size > KEEP) {
                    val worst = kept.minBy { it.score }
                    kept.remove(worst)
                    worst.bitmap.recycle()
                }
            }
            if (kept.isEmpty()) return@withContext null

            // Best frame, plus the best frame at least 2 samples away so the two differ.
            val ranked = kept.sortedByDescending { it.score }
            val best = ranked.first()
            val second = ranked.drop(1).firstOrNull { kotlin.math.abs(it.index - best.index) >= 2 }
            val chosen = listOfNotNull(best, second).sortedBy { it.index }

            val onScreen = chosen
                .flatMap { it.text.lines() }
                .map { it.trim() }
                .filter { it.length >= 3 }
                .distinct()
                .joinToString(" | ")
                .take(300)

            Picked(stitch(chosen), onScreen)
        } finally {
            kept.forEach { if (!it.bitmap.isRecycled) it.bitmap.recycle() }
            runCatching { faceDetector.close() }
            runCatching { recognizer.close() }
            runCatching { retriever.release() }
        }
    }

    private fun stitch(frames: List<Candidate>): ByteArray {
        val width = frames.sumOf { it.bitmap.width }
        val sheet = Bitmap.createBitmap(width, FRAME_HEIGHT, Bitmap.Config.RGB_565)
        val canvas = Canvas(sheet)
        var x = 0f
        for (f in frames) {
            canvas.drawBitmap(f.bitmap, x, 0f, null)
            x += f.bitmap.width
        }
        var quality = 75
        var bytes: ByteArray
        do {
            val out = ByteArrayOutputStream()
            sheet.compress(Bitmap.CompressFormat.JPEG, quality, out)
            bytes = out.toByteArray()
            quality -= 10
        } while (bytes.size > MAX_BYTES && quality >= 25)
        sheet.recycle()
        return bytes
    }

    private suspend fun rate(bmp: Bitmap, faces: FaceDetector, recognizer: TextRecognizer): Pair<Float, String> {
        val (sharp, brightness) = sharpnessAndBrightness(bmp)

        val faceShare = safe {
            val found = faces.process(InputImage.fromBitmap(bmp, 0)).await()
            val biggest = found.maxOfOrNull { it.boundingBox.width().toFloat() * it.boundingBox.height() } ?: 0f
            (biggest / (bmp.width * bmp.height)).coerceIn(0f, 0.5f) * 2f // 0..1
        } ?: 0f

        val text = safe { recognizer.process(InputImage.fromBitmap(bmp, 0)).await().text.trim() }.orEmpty()
        val textShare = (text.count { it.isLetterOrDigit() } / 30f).coerceIn(0f, 1f)

        val exposurePenalty = if (brightness < 30f || brightness > 235f) -1f else 0f
        return (sharp + faceShare * 2f + textShare * 1.5f + exposurePenalty) to text
    }

    /** Returns (sharpness 0..1, mean brightness 0..255) from a small grayscale copy. */
    private fun sharpnessAndBrightness(bmp: Bitmap): Pair<Float, Float> {
        val h = 128
        val w = (bmp.width * h / bmp.height.toFloat()).toInt().coerceAtLeast(8)
        val small = Bitmap.createScaledBitmap(bmp, w, h, true)
        val px = IntArray(w * h)
        small.getPixels(px, 0, w, 0, 0, w, h)
        if (small !== bmp) small.recycle()

        val gray = FloatArray(px.size) {
            val c = px[it]
            0.299f * ((c shr 16) and 0xFF) + 0.587f * ((c shr 8) and 0xFF) + 0.114f * (c and 0xFF)
        }
        val mean = gray.average().toFloat()

        // Variance of the Laplacian: higher means more edges, i.e. a sharper frame.
        var sum = 0.0
        var sumSq = 0.0
        var n = 0
        for (y in 1 until h - 1) for (x in 1 until w - 1) {
            val i = y * w + x
            val lap = 4 * gray[i] - gray[i - 1] - gray[i + 1] - gray[i - w] - gray[i + w]
            sum += lap
            sumSq += lap * lap
            n++
        }
        val variance = if (n > 0) (sumSq / n - (sum / n) * (sum / n)).toFloat() else 0f
        return (variance / 600f).coerceIn(0f, 1f) to mean
    }

    /** ML Kit failures (missing model, etc.) must not break captioning: just skip that signal. */
    private suspend fun <T> safe(block: suspend () -> T): T? = try {
        block()
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        null
    }
}
