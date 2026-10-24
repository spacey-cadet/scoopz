package com.scoopz.app.media

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.media.MediaMetadataRetriever
import android.net.Uri
import java.io.ByteArrayOutputStream

/**
 * Builds ONE jpeg "contact sheet" from a few frames of a video.
 *
 * The free NVIDIA vision models accept a single image per request, so we stitch
 * the frames side by side. Memory stays small: only one full frame is decoded at
 * a time and every bitmap is recycled as soon as it is drawn.
 */
object FrameStrip {
    private const val FRAME_HEIGHT = 512
    private val POSITIONS = doubleArrayOf(0.15, 0.50, 0.85)

    fun build(context: Context, uri: Uri): ByteArray? {
        val retriever = MediaMetadataRetriever()
        val frames = ArrayList<Bitmap>(POSITIONS.size)
        try {
            retriever.setDataSource(context, uri)
            val durationMs = retriever
                .extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull() ?: return null

            for (p in POSITIONS) {
                val timeUs = (durationMs * p * 1000).toLong()
                val full = retriever.getFrameAtTime(timeUs, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
                    ?: continue
                val w = (full.width.toFloat() * FRAME_HEIGHT / full.height).toInt().coerceAtLeast(1)
                val scaled = Bitmap.createScaledBitmap(full, w, FRAME_HEIGHT, true)
                if (scaled !== full) full.recycle()
                frames += scaled
            }
            if (frames.isEmpty()) return null

            val totalWidth = frames.sumOf { it.width }
            val sheet = Bitmap.createBitmap(totalWidth, FRAME_HEIGHT, Bitmap.Config.RGB_565)
            val canvas = Canvas(sheet)
            var x = 0f
            for (f in frames) {
                canvas.drawBitmap(f, x, 0f, null)
                x += f.width
                f.recycle()
            }
            frames.clear()

            val out = ByteArrayOutputStream()
            sheet.compress(Bitmap.CompressFormat.JPEG, 80, out)
            sheet.recycle()
            return out.toByteArray()
        } finally {
            frames.forEach { if (!it.isRecycled) it.recycle() }
            runCatching { retriever.release() }
        }
    }
}
