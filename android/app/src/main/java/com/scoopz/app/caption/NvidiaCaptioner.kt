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

    suspend fun caption(jpeg: ByteArray, title: String?): Result = withContext(Dispatchers.IO) {
        val key = settings.apiKey
        if (key.isBlank()) throw IOException("Add your NVIDIA API key in Settings")

        val b64 = Base64.encodeToString(jpeg, Base64.NO_WRAP)
        val prompt = buildPrompt(title)

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
                    resp.isSuccessful -> return@withContext parse(text)
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
                .put(JSONObject().put("type", "text").put("text", prompt))
                .put(
                    JSONObject().put("type", "image_url")
                        .put("image_url", JSONObject().put("url", "data:image/jpeg;base64,$b64"))
                )
        }
        return JSONObject()
            .put("model", settings.model)
            .put("max_tokens", 400)
            .put("temperature", 0.7)
            .put("stream", false)
            .put(
                "messages",
                JSONArray().put(JSONObject().put("role", "user").put("content", content))
            )
    }

    private fun buildPrompt(title: String?): String = buildString {
        append("The image shows three frames from one short video, left to right in time order. ")
        append("Write a punchy social media caption (max 2 sentences) describing what the video is about. ")
        append("Do not mention frames, the image, or time order. ")
        if (!title.isNullOrBlank()) append("Original title for context: \"${title.take(200)}\". ")
        append("Then suggest 5 relevant hashtags. ")
        append("Reply ONLY with JSON: {\"caption\":\"...\",\"hashtags\":[\"#tag1\",\"#tag2\"]}")
    }

    private fun parse(responseText: String): Result {
        val content = JSONObject(responseText)
            .getJSONArray("choices").getJSONObject(0)
            .getJSONObject("message").optString("content").trim()

        val start = content.indexOf('{')
        val end = content.lastIndexOf('}')
        if (start >= 0 && end > start) {
            runCatching {
                val j = JSONObject(content.substring(start, end + 1))
                val tags = j.optJSONArray("hashtags")
                val list = buildList { for (i in 0 until (tags?.length() ?: 0)) add(tags!!.optString(i)) }
                val caption = j.optString("caption").trim()
                if (caption.isNotEmpty()) {
                    return Result(caption, Hashtags.normalize(list).joinToString(" "))
                }
            }
        }
        // Model ignored the format: keep the prose, pull any inline #tags.
        val tags = Hashtags.normalize(Hashtags.extract(content))
        val caption = content.replace(Regex("""#\w+"""), "").trim().ifEmpty { throw IOException("Empty caption") }
        return Result(caption, tags.joinToString(" "))
    }

    private companion object {
        const val MAX_RETRIES = 3
    }
}
