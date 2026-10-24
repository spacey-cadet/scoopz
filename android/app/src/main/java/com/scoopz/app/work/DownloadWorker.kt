package com.scoopz.app.work

import android.content.ContentValues
import android.content.Context
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.scoopz.app.R
import com.scoopz.app.ScoopzApp
import com.scoopz.app.data.Status
import com.scoopz.app.error.ErrorExplainer
import com.scoopz.app.error.Stage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import okhttp3.Request
import java.io.IOException

class DownloadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun getForegroundInfo(): ForegroundInfo = foreground("Downloading video…", -1)

    override suspend fun doWork(): Result {
        val url = inputData.getString(KEY_URL) ?: return Result.failure()
        val g = ScoopzApp.graph
        val dao = g.db.dao()
        val ctx = applicationContext

        // Cap parallel downloads so RAM, radio and notification churn stay small.
        return gate.withPermit {
            var mediaUri: Uri? = null
            try {
                runCatching { setForeground(foreground("Downloading video…", -1)) }
                dao.setStatus(url, Status.DOWNLOADING)

                val meta = g.tikwm.resolve(url)
                dao.setMeta(url, meta.id, meta.author, meta.title)

                // Same video shared twice under different links: reuse the file.
                val dup = dao.findDone(meta.id)
                if (dup?.mediaUri != null && dup.sourceUrl != url) {
                    dao.setDone(url, dup.mediaUri)
                    return@withPermit Result.success()
                }

                mediaUri = createMediaStoreEntry(ctx, "${meta.author}_${meta.id}.mp4")
                streamTo(meta.playUrl, mediaUri, meta.author)

                ctx.contentResolver.update(
                    mediaUri,
                    ContentValues().apply { put(MediaStore.Video.Media.IS_PENDING, 0) },
                    null, null,
                )
                dao.setDone(url, mediaUri.toString())

                if (g.settings.autoCaption && g.settings.apiKey.isNotBlank()) {
                    Enqueue.caption(ctx, url)
                }
                Result.success()
            } catch (e: CancellationException) {
                mediaUri?.let { runCatching { ctx.contentResolver.delete(it, null, null) } }
                throw e
            } catch (e: Exception) {
                mediaUri?.let { runCatching { ctx.contentResolver.delete(it, null, null) } }
                if (runAttemptCount < MAX_ATTEMPTS - 1) {
                    Result.retry()
                } else {
                    dao.setStatus(url, Status.FAILED, ErrorExplainer.encode(e, Stage.DOWNLOAD))
                    Result.failure()
                }
            }
        }
    }

    private fun createMediaStoreEntry(ctx: Context, name: String): Uri {
        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, name.replace(Regex("[^A-Za-z0-9._-]"), "_"))
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH, Environment.DIRECTORY_MOVIES + "/Scoopz")
            put(MediaStore.Video.Media.IS_PENDING, 1)
        }
        return ctx.contentResolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw IOException("Could not create media entry")
    }

    /** Streams straight to storage in 64 KB chunks: the video is never held in memory. */
    private suspend fun streamTo(playUrl: String, dest: Uri, author: String) {
        val req = Request.Builder().url(playUrl).header("User-Agent", "Mozilla/5.0").build()
        ScoopzApp.graph.http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IOException("Download HTTP ${resp.code}")
            val body = resp.body ?: throw IOException("Empty download")
            val total = body.contentLength()
            val out = applicationContext.contentResolver.openOutputStream(dest)
                ?: throw IOException("Cannot open output")
            out.use { sink ->
                val buf = ByteArray(64 * 1024)
                var read = 0L
                var lastPct = -1
                body.byteStream().use { input ->
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        sink.write(buf, 0, n)
                        read += n
                        if (total > 0) {
                            val pct = (read * 100 / total).toInt()
                            if (pct >= lastPct + 10) {
                                lastPct = pct
                                runCatching { setForeground(foreground("Downloading @$author", pct)) }
                            }
                        }
                    }
                }
            }
        }
    }

    private fun foreground(text: String, pct: Int): ForegroundInfo {
        val n = NotificationCompat.Builder(applicationContext, ScoopzApp.CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("Scoopz")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .apply { if (pct >= 0) setProgress(100, pct, false) else setProgress(0, 0, true) }
            .build()
        val id = NOTIF_ID_BASE + (inputData.getString(KEY_URL)?.hashCode() ?: 0).and(0xFFFF)
        return if (Build.VERSION.SDK_INT >= 29) {
            ForegroundInfo(id, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(id, n)
        }
    }

    companion object {
        const val KEY_URL = "url"
        private const val MAX_ATTEMPTS = 3
        private const val NOTIF_ID_BASE = 1000
        private val gate = Semaphore(3)
    }
}
