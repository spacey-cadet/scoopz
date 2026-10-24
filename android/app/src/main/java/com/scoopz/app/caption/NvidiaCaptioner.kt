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

    suspend fun caption(jpeg: ByteArray, transcript: String = ""): Result = withContext(Dispatchers.IO) {
        val key = settings.apiKey
        if (key.isBlank()) throw IOException("Add your NVIDIA API key in Settings")

        val b64 = Base64.encodeToString(jpeg, Base64.NO_WRAP)
        val pick = CaptionStyle.next(settings)
        val prompt = CaptionPrompt.build(pick.povLabel, pick.styleMode, hasSpeech = transcript.isNotBlank(), transcript = transcript)

        var useInlineImg = false
        var attempt = 0
        while (true) {
            limiter.acquire()
            val body = buildBody(prompt, b64, useInlineImg)
            val req = Request.Builder()
                .url(settings.baseUrl + "/chat/completions")
                .header("Authorization", "Bearer $key")
                .header("Accept", "application/json")
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .build()

            http.newCall(req).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                when {
                    resp.isSuccessful -> return@withContext parse(text, transcript)
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
