package com.scoopz.app.work

import android.content.Context
import android.net.Uri
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.scoopz.app.ScoopzApp
import com.scoopz.app.caption.ContextCache
import com.scoopz.app.data.CaptionState
import com.scoopz.app.error.ErrorExplainer
import com.scoopz.app.error.Stage
import com.scoopz.app.media.AudioExtractor
import com.scoopz.app.media.FramePicker
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import java.io.File

/**
 * Caption pipeline. On the phone, in parallel: pick the best frame + read on-screen text + labels,
 * and transcribe the audio (if a transcription key is set). Then ONE text request writes the caption.
 * Vision is used only when asked for, switched on in Settings, or when there is almost no text info.
 * What was worked out locally is cached, so Redo is just the text request.
 */
class CaptionWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val url = inputData.getString(KEY_URL) ?: return Result.failure()
        val forceVision = inputData.getBoolean(KEY_VISION, false)
        val g = ScoopzApp.graph
        val dao = g.db.dao()
        val video = dao.get(url) ?: return Result.failure()
        val uri = video.mediaUri?.let(Uri::parse) ?: return Result.failure()
        val frames = if (g.settings.twoFrames) 2 else 1

        // Up to 3 captions at once. Each finishes and shows on its own card as soon as it is ready.
        return gate.withPermit {
            try {
                dao.setCaptionState(url, CaptionState.RUNNING)
                val cached = ContextCache.read(applicationContext, url)
                val wantVision = forceVision || g.settings.visionAlways
                val needTranscript = cached?.transcript == null && g.transcriber.enabled

                // Frame scan and transcription run side by side; either is skipped if cached or not needed.
                val (picked, transcriptOrNull) = coroutineScope {
                    val pick = async { if (cached == null || wantVision) FramePicker.pick(applicationContext, uri, frames) else null }
                    val speech = async { if (needTranscript) fetchTranscript(uri, url) else cached?.transcript }
                    pick.await() to speech.await()
                }
                if (cached == null && picked == null) throw IllegalStateException("Could not read frames from video")

                val onScreen = cached?.onScreenText ?: picked!!.onScreenText
                val labels = cached?.labels ?: picked!!.labels
                val transcript = transcriptOrNull.orEmpty()
                ContextCache.write(applicationContext, url, ContextCache.Info(onScreen, labels, transcriptOrNull))

                val useVision = wantVision || isThin(video.title, onScreen, transcript)
                val result = if (useVision) {
                    val shot = picked ?: FramePicker.pick(applicationContext, uri, frames)
                        ?: throw IllegalStateException("Could not read frames from video")
                    g.captioner.caption(shot.jpeg, video.title, video.author, onScreen, transcript)
                } else {
                    g.captioner.captionText(video.title, video.author, onScreen, labels, transcript)
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

    /**
     * Extracts the audio track and transcribes it. Returns "" when there is nothing to transcribe,
     * or null when it failed (so it is tried again next time). A failure never blocks the caption.
     */
    private suspend fun fetchTranscript(uri: Uri, url: String): String? {
        val g = ScoopzApp.graph
        val out = File(applicationContext.cacheDir, "audio_${url.hashCode()}.m4a")
        return try {
            if (!AudioExtractor.extract(applicationContext, uri, out)) return ""   // no audio track
            if (out.length() > MAX_AUDIO_BYTES) return ""                          // too big to upload
            val text = g.transcriber.transcribe(out)
            g.settings.sttLastError = ""
            text
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            g.settings.sttLastError = e.message ?: "Transcription failed"
            null
        } finally {
            out.delete()
        }
    }

    /** True when there is almost nothing to write from: no real title words, no readable text, no speech. */
    private fun isThin(title: String?, onScreenText: String, transcript: String): Boolean {
        val words = (title ?: "").replace(Regex("""#\w+"""), "").trim()
        return words.length < 12 && onScreenText.length < 8 && transcript.length < 20
    }

    companion object {
        const val KEY_URL = "url"
        const val KEY_VISION = "vision"
        private const val MAX_AUDIO_BYTES = 24L * 1024 * 1024
        private val gate = Semaphore(3)
    }
}
