package com.scoopz.app.work

import android.content.Context
import android.net.Uri
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.scoopz.app.ScoopzApp
import com.scoopz.app.data.CaptionState
import com.scoopz.app.media.FrameStrip
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext

class CaptionWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val url = inputData.getString(KEY_URL) ?: return Result.failure()
        val g = ScoopzApp.graph
        val dao = g.db.dao()
        val video = dao.get(url) ?: return Result.failure()
        val uri = video.mediaUri?.let(Uri::parse) ?: return Result.failure()

        // One caption at a time: keeps frame bitmaps to a single video and respects the rate limit.
        return gate.withPermit {
            try {
                dao.setCaptionState(url, CaptionState.RUNNING)
                val jpeg = withContext(Dispatchers.IO) { FrameStrip.build(applicationContext, uri) }
                    ?: throw IllegalStateException("Could not read frames from video")
                val result = g.captioner.caption(jpeg, video.title)
                dao.setCaption(url, result.caption, result.hashtags)
                Result.success()
            } catch (e: CancellationException) {
                dao.setCaptionState(url, CaptionState.NONE)
                throw e
            } catch (e: Exception) {
                dao.setCaptionState(url, CaptionState.FAILED, e.message ?: "Caption failed")
                Result.failure()
            }
        }
    }

    companion object {
        const val KEY_URL = "url"
        private val gate = Semaphore(1)
    }
}
