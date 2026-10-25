package com.scoopz.app.work

import android.content.Context
import android.net.Uri
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.scoopz.app.ScoopzApp
import com.scoopz.app.data.CaptionState
import com.scoopz.app.error.ErrorExplainer
import com.scoopz.app.error.Stage
import com.scoopz.app.media.FramePicker
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit

class CaptionWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val url = inputData.getString(KEY_URL) ?: return Result.failure()
        val forceVision = inputData.getBoolean(KEY_VISION, false)
        val g = ScoopzApp.graph
        val dao = g.db.dao()
        val video = dao.get(url) ?: return Result.failure()
        val uri = video.mediaUri?.let(Uri::parse) ?: return Result.failure()

        // Up to 3 captions at once. Each finishes and shows on its own card as soon as it is ready.
        return gate.withPermit {
            try {
                dao.setCaptionState(url, CaptionState.RUNNING)
                // On-device step: best frame, on-screen text (OCR) and image labels.
                val picked = FramePicker.pick(applicationContext, uri, if (g.settings.twoFrames) 2 else 1)
                    ?: throw IllegalStateException("Could not read frames from video")

                // Text first (seconds). Vision only when asked for, switched on in Settings,
                // or when the video has too little text info to write from.
                val useVision = forceVision || g.settings.visionAlways || isThin(video.title, picked.onScreenText)
                val result = if (useVision) {
                    g.captioner.caption(picked.jpeg, video.title, video.author, picked.onScreenText)
                } else {
                    g.captioner.captionText(video.title, video.author, picked.onScreenText, picked.labels)
                }
                dao.setCaption(url, result.caption, result.hashtags)
                Result.success()
            } catch (e: CancellationException) {
                dao.setCaptionState(url, CaptionState.NONE)
                throw e
            } catch (e: Exception) {
                dao.setCaptionState(url, CaptionState.FAILED, ErrorExplainer.encode(e, Stage.CAPTION))
                Result.failure()
            }
        }
    }

    /** True when there is almost nothing to write from: no real title words and no readable on-screen text. */
    private fun isThin(title: String?, onScreenText: String): Boolean {
        val words = (title ?: "").replace(Regex("""#\w+"""), "").trim()
        return words.length < 12 && onScreenText.length < 8
    }

    companion object {
        const val KEY_URL = "url"
        const val KEY_VISION = "vision"
        private val gate = Semaphore(3)
    }
}
