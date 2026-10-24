package com.scoopz.app.caption

import android.util.Base64
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
import org.json.JSONObject
import java.io.IOException
import java.io.InterruptedIOException
import java.util.concurrent.TimeUnit

/**
 * Captions the picked frames through NVIDIA's OpenAI-compatible endpoint
 * (https://integrate.api.nvidia.com/v1 by default, free key from build.nvidia.com).
 *
 * A request that times out or fails is retried on the backup model instead of
 * making the user wait, so a stuck model costs ~45 s, not minutes.
 */
class NvidiaCaptioner(
    private val http: OkHttpClient,
    private val settings: Settings,
    private val limiter: RateLimiter,
) {
    data class Result(val caption: String, val hashtags: String)

    /** This model could not answer; trying another model may work. Other IOExceptions are fatal. */
    private class ModelFailed(message: String) : IOException(message)

    private val fastHttp: OkHttpClient = http.newBuilder()
        .readTimeout(45, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .callTimeout(60, TimeUnit.SECONDS)
        .build()

    suspend fun caption(
        jpeg: ByteArray,
        title: String?,
        author: String?,
        onScreenText: String,
    ): Result = withContext(Dispatchers.IO) {
        val key = settings.apiKey
        if (key.isBlank()) throw IOException("Add your NVIDIA API key in Settings")

        val b64 = Base64.encodeToString(jpeg, Base64.NO_WRAP)
        val pick = CaptionStyle.next(settings)
        val prompt = CaptionPrompt.build(pick.povLabel, pick.styleMode, title, author, onScreenText)
        // Title, hashtags and on-screen text also seed the video-specific hashtags.
        val contextText = listOfNotNull(title, onScreenText).joinToString(" ")

        val models = listOf(settings.model, settings.backupModel)
            .map { it.trim() }.filter { it.isNotEmpty() }.distinct()

        // Keep every model's error so the card shows why the main model failed, not just the backup.
        val failures = mutableListOf<String>()
        for (model in models) {
            try {
                return@withContext callModel(model, key, prompt, b64, contextText)
            } catch (e: ModelFailed) {
                failures += e.message.orEmpty().take(140)
            }
        }
        throw IOException(failures.joinToString("\n").ifBlank { "No model configured" })
    }

    private suspend fun callModel(model: String, key: String, prompt: String, b64: String, contextText: String): Result {
        var inlineImg = false
        var rateLimited = 0
        while (true) {
            limiter.acquire()
            val req = Request.Builder()
                .url(settings.baseUrl + "/chat/completions")
                .header("Authorization", "Bearer $key")
                .header("Accept", "application/json")
                .post(buildBody(model, prompt, b64, inlineImg).toString().toRequestBody("application/json".toMediaType()))
                .build()

            val resp = try {
                fastHttp.newCall(req).execute()
            } catch (e: CancellationException) {
                throw e
            } catch (e: InterruptedIOException) {
                throw ModelFailed("$model took too long to answer")
            } catch (e: IOException) {
                throw ModelFailed("Network problem: ${e.message}")
            }

            val code = resp.code
            val retryAfter = resp.header("Retry-After")?.toLongOrNull()
            val text = resp.use { it.body?.string().orEmpty() }

            when {
                code in 200..299 -> return parse(text, contextText)
                code == 429 && rateLimited < 2 -> {
                    rateLimited++
                    val wait = ((retryAfter ?: 5L).coerceIn(1L, 20L)) * 1000
                    limiter.penalize(wait)
                    delay(wait)
                }
                code == 429 -> throw ModelFailed("$model is rate limited, try again in a minute")
                code == 400 && !inlineImg -> inlineImg = true // some models want the <img src="data:..."> form
                code == 401 -> throw IOException("NVIDIA rejected your API key (HTTP 401). Check it in Settings.")
                code == 403 -> throw ModelFailed("$model is not enabled for your key (HTTP 403). Open it on build.nvidia.com and use Try API once.")
                code == 404 -> throw ModelFailed("$model is not available to your account (HTTP 404). Choose another model in Settings.")
                else -> throw ModelFailed("$model returned HTTP $code: ${text.take(120)}")
            }
        }
    }

    private fun buildBody(model: String, prompt: String, b64: String, inlineImg: Boolean): JSONObject {
        val content: Any = if (inlineImg) {
            "$prompt <img src=\"data:image/jpeg;base64,$b64\" />"
        } else {
            JSONArray()
                .put(
                    JSONObject().put("type", "image_url")
                        .put("image_url", JSONObject().put("url", "data:image/jpeg;base64,$b64"))
                )
                .put(JSONObject().put("type", "text").put("text", prompt))
        }
        return JSONObject()
            .put("model", model)
            .put("max_tokens", 300)
            .put("temperature", 0.9)
            .put("stream", false)
            .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", content)))
    }

    private fun parse(responseText: String, contextText: String): Result {
        val text = try {
            JSONObject(responseText)
                .getJSONArray("choices").getJSONObject(0)
                .getJSONObject("message").optString("content").trim()
        } catch (e: org.json.JSONException) {
            throw ModelFailed("Unreadable response from NVIDIA")
        }

        val opts = setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE)
        val captionRaw = Regex("""CAPTION:\s*(.+?)(?=HASHTAGS?:|$)""", opts).find(text)?.groupValues?.get(1)?.trim()
            ?: text.substringBefore("HASHTAG", text).trim()
        val hashtagsRaw = Regex("""HASHTAGS?:\s*(.+?)$""", opts).find(text)?.groupValues?.get(1)?.trim().orEmpty()

        // Tags the model put inside the caption, and tags from the TikTok title, are good candidates too.
        val tagRe = Regex("""#\w{2,30}""")
        val inlineTags = tagRe.findAll(captionRaw).joinToString(" ") { it.value }
        val titleTags = tagRe.findAll(contextText).take(4).joinToString(" ") { it.value }

        val caption = CaptionStyle.cleanCaption(captionRaw)
        if (caption.isEmpty()) throw ModelFailed("Empty caption from model")
        return Result(caption, Hashtags.normalize("$hashtagsRaw $inlineTags $titleTags", caption, contextText))
    }
}
