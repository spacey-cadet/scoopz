package com.scoopz.app.caption

import android.os.SystemClock
import com.scoopz.app.data.Settings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * Collects text-caption requests from the caption workers and sends them to the API together,
 * so one request writes captions for up to [Settings.batchSize] videos instead of one each.
 *
 * A batch is sent when:
 *  - enough videos have gathered (the batch size in Settings), or
 *  - the oldest waiting video has waited [MAX_WAIT_MS] (so a lone video is never stuck), or
 *  - someone tapped Caption / Redo ([Request.urgent]): after a short [URGENT_GRACE_MS] so several
 *    taps made together still travel as one request.
 * Whenever a send happens, everything already waiting rides along (up to the batch size per request).
 *
 * Runs in the app scope, so one worker being cancelled never cancels the batch the others depend on.
 */
class CaptionBatcher(
    private val scope: CoroutineScope,
    private val captioner: NvidiaCaptioner,
    private val settings: Settings,
) {
    class Request(
        val input: NvidiaCaptioner.TextInput,
        val urgent: Boolean,
    ) {
        internal val result = CompletableDeferred<NvidiaCaptioner.Result>()
        internal val queuedAt = SystemClock.elapsedRealtime()
        internal val dueAt get() = queuedAt + if (urgent) URGENT_GRACE_MS else MAX_WAIT_MS
    }

    private val lock = Any()
    private val queue = ArrayList<Request>()
    private var timer: Job? = null

    /** Suspends until this video's caption is written. Batching is off when the batch size is 1. */
    suspend fun submit(input: NvidiaCaptioner.TextInput, urgent: Boolean): NvidiaCaptioner.Result {
        if (batchSize() <= 1) return captioner.captionText(input.title, input.author, input.onScreenText, input.labels, input.transcript)

        val req = Request(input, urgent)
        val full = synchronized(lock) {
            queue += req
            val ready = drainFull()
            rearm()
            ready
        }
        full.forEach(::launchBatch)

        try {
            return req.result.await()
        } catch (e: CancellationException) {
            req.result.cancel() // the worker was cancelled: don't spend part of a request on it
            throw e
        }
    }

    private fun batchSize() = settings.batchSize.coerceIn(1, Settings.MAX_BATCH_SIZE)

    /** Removes and returns every complete batch. Caller holds [lock]. */
    private fun drainFull(): List<List<Request>> {
        queue.removeAll { it.result.isCompleted }
        val n = batchSize().coerceAtLeast(2)
        val out = ArrayList<List<Request>>()
        while (queue.size >= n) {
            out.add(ArrayList(queue.subList(0, n)))
            queue.subList(0, n).clear()
        }
        return out
    }

    /** Waits for the earliest deadline in the queue, then sends. Caller holds [lock]. */
    private fun rearm() {
        timer?.cancel()
        timer = null
        queue.removeAll { it.result.isCompleted }
        val due = queue.minOfOrNull { it.dueAt } ?: return
        val wait = (due - SystemClock.elapsedRealtime()).coerceAtLeast(0L)
        timer = scope.launch {
            delay(wait)
            flushDue()
        }
    }

    /** Timer fired: if something is really due, send everything waiting; otherwise this timer was stale. */
    private fun flushDue() {
        val batches = synchronized(lock) {
            queue.removeAll { it.result.isCompleted }
            val now = SystemClock.elapsedRealtime()
            if (queue.none { it.dueAt <= now }) {
                rearm()
                return@synchronized emptyList<List<Request>>()
            }
            timer = null
            val all = ArrayList(queue)
            queue.clear()
            all.chunked(batchSize().coerceAtLeast(2))
        }
        batches.forEach(::launchBatch)
    }

    private fun launchBatch(batch: List<Request>) {
        scope.launch(Dispatchers.IO) {
            val live = batch.filterNot { it.result.isCompleted } // skip videos whose worker was cancelled
            when {
                live.isEmpty() -> Unit
                live.size == 1 -> single(live[0])
                else -> try {
                    val answers = captioner.captionTextBatch(live.map { it.input })
                    live.forEachIndexed { i, req ->
                        val answer = answers.getOrNull(i)
                        // The model skipped or garbled this one: retry just it on its own.
                        if (answer != null) req.result.complete(answer) else single(req)
                    }
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    // The whole request failed on every model. Retrying each video alone would only
                    // multiply the same failure, so every card shows the error and can be redone.
                    live.forEach { it.result.completeExceptionally(e) }
                }
            }
        }
    }

    private suspend fun single(req: Request) {
        val i = req.input
        try {
            req.result.complete(captioner.captionText(i.title, i.author, i.onScreenText, i.labels, i.transcript))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            req.result.completeExceptionally(e)
        }
    }

    companion object {
        /** A video that is not in a hurry waits at most this long for others to join its request. */
        const val MAX_WAIT_MS = 20_000L

        /** A tapped Caption / Redo waits this long so taps made together share a request. */
        const val URGENT_GRACE_MS = 1_500L
    }
}
