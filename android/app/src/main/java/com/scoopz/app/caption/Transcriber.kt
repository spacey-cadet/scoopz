package com.scoopz.app.caption

import com.scoopz.app.data.Settings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Speech to text through any OpenAI-compatible `/audio/transcriptions` endpoint.
 * Default is Groq's hosted Whisper (free key from console.groq.com). NVIDIA's own speech models
 * are only offered over gRPC, so they can't be called like the chat models.
 * Optional: with no key saved, transcription is simply skipped.
 */
class Transcriber(
    http: OkHttpClient,
    private val settings: Settings,
    private val limiter: RateLimiter,
) {
    private val client = http.newBuilder()
        .readTimeout(45, TimeUnit.SECONDS)
        .writeTimeout(45, TimeUnit.SECONDS)
        .callTimeout(70, TimeUnit.SECONDS)
        .build()

    val enabled: Boolean get() = settings.sttKey.isNotBlank()

    /** Returns the cleaned transcript ("" if there was no real speech). Throws IOException on failure. */
    suspend fun transcribe(audio: File): String = withContext(Dispatchers.IO) {
        repeat(2) { attempt ->
            limiter.acquire()
            val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("model", settings.sttModel)
                .addFormDataPart("response_format", "json")
                .addFormDataPart("temperature", "0")
                .addFormDataPart("file", "audio.m4a", audio.asRequestBody("audio/mp4".toMediaType()))
                .build()
            val req = Request.Builder()
                .url(settings.sttBaseUrl + "/audio/transcriptions")
                .header("Authorization", "Bearer ${settings.sttKey}")
                .post(body)
                .build()
            try {
                client.newCall(req).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    when {
                        resp.isSuccessful -> return@withContext clean(parseText(text))
                        resp.code == 429 && attempt == 0 ->
                            limiter.penalize(((resp.header("Retry-After")?.toLongOrNull() ?: 5L).coerceIn(1L, 15L)) * 1000)
                        resp.code == 401 -> throw IOException("Transcription key rejected (HTTP 401). Check it in Settings.")
                        resp.code == 404 -> throw IOException("Transcription model or URL not found (HTTP 404). Check Settings.")
                        else -> throw IOException("Transcription HTTP ${resp.code}: ${text.take(100)}")
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: java.io.InterruptedIOException) {
                throw IOException("Transcription took too long")
            }
        }
        throw IOException("Transcription is busy. Try again in a minute.")
    }

    private fun parseText(body: String): String = try {
        JSONObject(body).optString("text")
    } catch (e: JSONException) {
        throw IOException("Unreadable transcription answer")
    }

    /** Whisper invents filler on silence or music ("Thank you."). Treat that as no speech. */
    private fun clean(raw: String): String {
        val t = raw.trim()
        val letters = t.lowercase().filter { it.isLetter() }
        if (letters.length < 12) return ""
        if (letters in FILLER) return ""
        return t.take(1200)
    }

    private companion object {
        val FILLER = setOf("thankyouforwatching", "thanksforwatching", "pleasesubscribe", "subtitlesbytheamaraorgcommunity")
    }
}
