package com.scoopz.app.caption

import android.util.Base64
import com.scoopz.app.data.Settings
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

/**
 * Captions a frame strip through NVIDIA's OpenAI-compatible endpoint
 * (https://integrate.api.nvidia.com/v1 by default, free key from build.nvidia.com).
 */
class NvidiaCaptioner(
    private val http: OkHttpClient,
    private val settings: Settings,
    private val limiter: RateLimiter,
) {
    data class Result(val caption: String, val hashtags: String)

    /** Vision calls on the free tier can take over a minute, so allow far longer than normal calls. */
    private val slowHttp: OkHttpClient = http.newBuilder()
        .readTimeout(150, java.util.concurrent.TimeUnit.SECONDS)
        .writeTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .callTimeout(180, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    suspend fun caption(jpeg: ByteArray, transcript: String = ""): Result = withContext(Dispatchers.IO) {
        val key = settings.apiKey
        if (key.isBlank()) throw IOException("Add your NVIDIA API key in Settings")

        val b64 = Base64.encodeToString(jpeg, Base64.NO_WRAP)
        val pick = CaptionStyle.next(settings)
        val prompt = CaptionPrompt.build(pick.povLabel, pick.styleMode, hasSpeech = transcript.isNotBlank(), transcript = transcript)

        var useInlineImg = false
        var attempt = 0
        var result: Result? = null
        while (result == null) {
            limiter.acquire()
            val body = buildBody(prompt, b64, useInlineImg)
            val req = Request.Builder()
                .url(settings.baseUrl + "/chat/completions")
                .header("Authorization", "Bearer $key")
                .header("Accept", "application/json")
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .build()

            val response = try {
                slowHttp.newCall(req).execute()
            } catch (e: java.io.InterruptedIOException) {
                // Free-tier queues can stall; retry before giving up.
                if (attempt < MAX_RETRIES) {
                    attempt++
                    delay(2_000)
                    continue
                }
                throw IOException("NVIDIA took too long to answer. Try again, or pick a smaller/faster model in Settings.")
            }
            response.use { resp ->
                val text = resp.body?.string().orEmpty()
                when {
                    resp.isSuccessful -> {
                        result = parse(text, transcript)
                    }
                    resp.code == 429 && attempt < MAX_RETRIES -> {
                        val wait = (resp.header("Retry-After")?.toLongOrNull() ?: 5L) * 1000
                        limiter.penalize(wait)
                        attempt++
                        delay(wait)
                    }
                    resp.code == 400 && !useInlineImg -> {
                        // Some NVIDIA vision models want the <img src="data:..."> text form.
                        useInlineImg = true
                    }
                    resp.code == 401 || resp.code == 403 ->
                        throw IOException("NVIDIA rejected the key/model (HTTP ${resp.code}). Enable the model on build.nvidia.com.")
                    else -> throw IOException("NVIDIA HTTP ${resp.code}: ${text.take(160)}")
                }
            }
        }
        checkNotNull(result)
    }

    private fun buildBody(prompt: String, b64: String, inlineImg: Boolean): JSONObject {
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
            .put("model", settings.model)
            .put("max_tokens", 500)
            .put("temperature", 0.9)
            .put("stream", false)
            .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", content)))
    }

    private fun parse(responseText: String, transcript: String): Result {
        val text = JSONObject(responseText)
            .getJSONArray("choices").getJSONObject(0)
            .getJSONObject("message").optString("content").trim()

        val opts = setOf(RegexOption.DOT_MATCHES_ALL, RegexOption.IGNORE_CASE)
        val captionRaw = Regex("""CAPTION:\s*(.+?)(?=HASHTAGS?:|$)""", opts).find(text)?.groupValues?.get(1)?.trim()
            ?: text.substringBefore("HASHTAG", text).trim()
        val hashtagsRaw = Regex("""HASHTAGS?:\s*(.+?)$""", opts).find(text)?.groupValues?.get(1)?.trim().orEmpty()

        val caption = CaptionStyle.cleanCaption(captionRaw)
        if (caption.isEmpty()) throw IOException("Empty caption from model")
        return Result(caption, Hashtags.normalize(hashtagsRaw, caption, transcript))
    }

    private companion object {
        const val MAX_RETRIES = 3
    }
}
