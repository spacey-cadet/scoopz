package com.scoopz.app.error

import com.scoopz.app.caption.RateLimiter
import com.scoopz.app.data.Settings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

sealed interface AiState {
    data object Loading : AiState
    data class Done(val text: String) : AiState
    data class Failed(val message: String) : AiState
}

/**
 * Asks an NVIDIA model to explain an error in plain words. Only the error text is sent
 * (secrets are redacted): no video, no link, no API key. Uses the main model, then the backup.
 */
class ErrorAi(
    http: OkHttpClient,
    private val settings: Settings,
    private val limiter: RateLimiter,
) {
    private val client = http.newBuilder()
        .readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(40, TimeUnit.SECONDS)
        .build()

    suspend fun explain(stage: Stage, rawError: String): String = withContext(Dispatchers.IO) {
        val key = settings.apiKey
        if (key.isBlank()) throw IOException("Add your NVIDIA API key in Settings to use AI explanations.")

        val prompt = buildPrompt(stage, redact(rawError))
        val models = listOf(settings.model, settings.backupModel).map { it.trim() }.filter { it.isNotEmpty() }.distinct()

        for (model in models) {
            try {
                limiter.acquire()
                val body = JSONObject()
                    .put("model", model)
                    .put("max_tokens", 220)
                    .put("temperature", 0.3)
                    .put("stream", false)
                    .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", prompt)))
                val req = Request.Builder()
                    .url(settings.baseUrl + "/chat/completions")
                    .header("Authorization", "Bearer $key")
                    .post(body.toString().toRequestBody("application/json".toMediaType()))
                    .build()
                client.newCall(req).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    if (resp.isSuccessful) {
                        val answer = JSONObject(text).getJSONArray("choices").getJSONObject(0)
                            .getJSONObject("message").optString("content").trim()
                        if (answer.isNotEmpty()) return@withContext answer.replace("**", "")
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // try the next model
            }
        }
        throw IOException("AI explanation isn't available right now. The suggested fix on the card still applies.")
    }

    private fun buildPrompt(stage: Stage, error: String): String = """
        You are the error helper inside Scoopz, an Android app that downloads TikTok videos and writes captions for them with NVIDIA vision models.
        The ${stage.name.lowercase()} step failed with this error:
        $error

        Explain it for a non-technical user. First say what most likely happened in 1-2 sentences, then give at most 3 short numbered fix steps.
        The app's Settings screen has only: NVIDIA API key, vision model, backup vision model, base URL, max requests per minute, auto-caption. Do not mention anything else.
        No jargon. Maximum 90 words.
    """.trimIndent()

    /** Never send secrets or links off the phone, even inside an error message. */
    private fun redact(s: String): String = s
        .replace(Regex("""(?i)bearer\s+\S+"""), "Bearer ***")
        .replace(Regex("""nvapi-\S+"""), "***")
        .replace(Regex("""https?://\S+"""), "<url>")
        .take(600)
}
