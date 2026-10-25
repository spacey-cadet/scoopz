package com.scoopz.app.models

import android.graphics.Bitmap
import android.graphics.Color
import android.os.SystemClock
import android.util.Base64
import com.scoopz.app.caption.RateLimiter
import com.scoopz.app.data.Settings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InterruptedIOException
import java.util.concurrent.TimeUnit

/** Result of testing one model. */
sealed interface Probe {
    data object Pending : Probe
    data object Testing : Probe
    data class Ok(val ms: Long) : Probe
    data class Wrong(val ms: Long, val answer: String) : Probe
    data class Failed(val reason: String) : Probe
}

class AuthFailed : IOException("NVIDIA rejected your API key (HTTP 401). Check it in Settings.")

/**
 * Finds which models actually work on the user's NVIDIA key, and how fast.
 *  - Vision models get a tiny solid-red picture and "what colour is this?". A correct answer
 *    proves the model takes images.
 *  - Text models get "reply with one word" and are just timed.
 */
class ModelFinder(
    http: OkHttpClient,
    private val settings: Settings,
    private val limiter: RateLimiter,
) {
    private val client = http.newBuilder()
        .readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(35, TimeUnit.SECONDS)
        .build()

    /** Model ids NVIDIA says this key can see. Throws [AuthFailed] on a bad key. */
    suspend fun listModels(): List<String> = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url(settings.baseUrl + "/models")
            .header("Authorization", "Bearer ${settings.apiKey}")
            .build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (resp.code == 401) throw AuthFailed()
            if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
            val data = try {
                JSONObject(text).getJSONArray("data")
            } catch (e: JSONException) {
                throw IOException("unreadable model list")
            }
            (0 until data.length()).map { data.getJSONObject(it).optString("id") }.filter { it.isNotBlank() }
        }
    }

    /** Narrows a long catalog to models that look like they accept images. The real test comes later. */
    fun candidates(all: List<String>): List<String> =
        all.filter { id ->
            val m = id.lowercase()
            VISION_HINTS.any { it in m } && NOT_CHAT.none { it in m }
        }.distinct().take(MAX_CANDIDATES)

    /** Small chat models first: they are the fastest for writing captions from text. */
    fun textCandidates(all: List<String>): List<String> =
        all.filter { id ->
            val m = id.lowercase()
            ("instruct" in m || "chat" in m) &&
                VISION_HINTS.none { it in m } && NOT_CHAT.none { it in m } && TEXT_SKIP.none { it in m }
        }.distinct()
            .sortedBy { id -> if (SMALL_HINTS.any { it in id.lowercase() }) 0 else 1 }
            .take(MAX_TEXT_CANDIDATES)

    /**
     * Tests [model]. [textOnly] = no image (just times a one-word answer).
     * Throws [AuthFailed] on a bad key.
     */
    suspend fun probe(model: String, textOnly: Boolean = false): Probe {
        var inline = false
        var rateRetried = false
        repeat(4) {
            when (val a = call(model, inline, textOnly)) {
                is Call.Failed -> return a.probe
                is Call.Response -> {
                    val code = a.code
                    when {
                        code in 200..299 -> return judge(a.body, a.ms, textOnly)
                        code == 401 -> throw AuthFailed()
                        code == 403 -> return Probe.Failed("Not enabled for your key. Open this model on build.nvidia.com and tap Try API once.")
                        code == 404 -> return Probe.Failed("Not available on your account.")
                        code == 429 && !rateRetried -> {
                            rateRetried = true
                            delay((a.retryAfter ?: 5L).coerceIn(1L, 15L) * 1000)
                        }
                        code == 429 -> return Probe.Failed("Rate limited. Try again in a minute.")
                        (code == 400 || code == 422) && !inline && !textOnly -> inline = true // try the <img> text form once
                        code == 400 || code == 422 ->
                            return Probe.Failed(
                                if (textOnly) "Not a chat model, or it uses a different request format (HTTP $code)."
                                else "Not a vision model, or it uses a different request format (HTTP $code)."
                            )
                        else -> return Probe.Failed("HTTP $code")
                    }
                }
            }
        }
        return Probe.Failed("No answer")
    }

    private sealed interface Call {
        class Response(val code: Int, val body: String, val ms: Long, val retryAfter: Long?) : Call
        class Failed(val probe: Probe) : Call
    }

    private suspend fun call(model: String, inline: Boolean, textOnly: Boolean): Call = withContext(Dispatchers.IO) {
        limiter.acquire()
        val req = Request.Builder()
            .url(settings.baseUrl + "/chat/completions")
            .header("Authorization", "Bearer ${settings.apiKey}")
            .header("Accept", "application/json")
            .post(buildBody(model, inline, textOnly).toString().toRequestBody("application/json".toMediaType()))
            .build()
        val started = SystemClock.elapsedRealtime()
        try {
            client.newCall(req).execute().use { resp ->
                val body = resp.body?.string().orEmpty()
                Call.Response(
                    resp.code, body, SystemClock.elapsedRealtime() - started,
                    resp.header("Retry-After")?.toLongOrNull(),
                )
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: InterruptedIOException) {
            Call.Failed(Probe.Failed("Timed out (over 30 s)"))
        } catch (e: IOException) {
            Call.Failed(Probe.Failed("Network problem: ${e.message}"))
        }
    }

    private fun buildBody(model: String, inline: Boolean, textOnly: Boolean): JSONObject {
        val content: Any = when {
            textOnly -> TEXT_QUESTION
            inline -> "$QUESTION <img src=\"data:image/jpeg;base64,$testImage\" />"
            else -> JSONArray()
                .put(
                    JSONObject().put("type", "image_url")
                        .put("image_url", JSONObject().put("url", "data:image/jpeg;base64,$testImage"))
                )
                .put(JSONObject().put("type", "text").put("text", QUESTION))
        }
        return JSONObject()
            .put("model", model)
            .put("max_tokens", 12)
            .put("temperature", 0)
            .put("stream", false)
            .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", content)))
    }

    private fun judge(body: String, ms: Long, textOnly: Boolean): Probe {
        val answer = try {
            JSONObject(body).getJSONArray("choices").getJSONObject(0)
                .getJSONObject("message").optString("content").trim()
        } catch (e: JSONException) {
            return Probe.Failed("Unreadable answer")
        }
        if (textOnly) return if (answer.isNotEmpty()) Probe.Ok(ms) else Probe.Failed("Empty answer")
        return if ("red" in answer.lowercase()) Probe.Ok(ms) else Probe.Wrong(ms, answer.take(40))
    }

    private val testImage: String by lazy {
        val bmp = Bitmap.createBitmap(224, 224, Bitmap.Config.RGB_565)
        bmp.eraseColor(Color.rgb(220, 20, 20))
        val out = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, 80, out)
        bmp.recycle()
        Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }

    companion object {
        private const val QUESTION = "What is the main color of this image? Reply with one word."
        private const val TEXT_QUESTION = "Reply with the single word: ready"
        private const val MAX_CANDIDATES = 16
        private const val MAX_TEXT_CANDIDATES = 8

        private val VISION_HINTS = listOf(
            "vision", "-vl", "vl-", "vlm", "vila", "neva", "llava", "pixtral", "paligemma", "kosmos",
            "fuyu", "multimodal", "gemma-3", "llama-4", "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
        )
        private val NOT_CHAT = listOf(
            "embed", "rerank", "guard", "reward", "safety", "tts", "asr", "riva", "retriev", "clip",
            "segment", "diffusion", "flux", "stable", "parse", "ocr", "translate",
        )
        private val TEXT_SKIP = listOf("coder", "code", "math")
        private val SMALL_HINTS = listOf("1b", "3b", "7b", "8b", "9b", "mini", "small", "nano", "lite")

        /** Used only when the model list can't be read. Names are guesses: failures are shown, not hidden. */
        val SEED = listOf(
            "meta/llama-3.2-11b-vision-instruct",
            "meta/llama-3.2-90b-vision-instruct",
            "microsoft/phi-3.5-vision-instruct",
            "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            "meta/llama-4-scout-17b-16e-instruct",
            "meta/llama-4-maverick-17b-128e-instruct",
            "google/gemma-3-27b-it",
            "mistralai/mistral-small-3.1-24b-instruct-2503",
        )
        val TEXT_SEED = listOf(
            "meta/llama-3.1-8b-instruct",
            "meta/llama-3.2-3b-instruct",
            "mistralai/mistral-7b-instruct-v0.3",
            "meta/llama-3.3-70b-instruct",
        )
    }
}
