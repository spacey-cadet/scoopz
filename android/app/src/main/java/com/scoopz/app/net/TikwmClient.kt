package com.scoopz.app.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException

class TikwmClient(private val http: OkHttpClient) {

    data class Meta(
        val id: String,
        val author: String,
        val title: String,
        val playUrl: String,
    )

    /** Resolves a shared TikTok link into a direct mp4 URL (HD when available). */
    suspend fun resolve(url: String): Meta = withContext(Dispatchers.IO) {
        var last: Exception? = null
        repeat(ATTEMPTS) { attempt ->
            try {
                val req = Request.Builder()
                    .url(API)
                    .header("User-Agent", UA)
                    .post(FormBody.Builder().add("url", url).add("hd", "1").build())
                    .build()
                http.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) throw IOException("tikwm HTTP ${resp.code}")
                    val json = JSONObject(resp.body?.string().orEmpty())
                    if (json.optInt("code", -1) != 0) {
                        throw IOException(json.optString("msg", "tikwm refused the link"))
                    }
                    val d = json.getJSONObject("data")
                    val id = d.optString("id").trim()
                    val play = d.optString("hdplay").ifBlank { d.optString("play") }.trim()
                    if (id.isEmpty() || play.isEmpty()) throw IOException("No video in response")
                    val authorObj = d.optJSONObject("author")
                    val author = authorObj?.optString("unique_id")?.ifBlank { null }
                        ?: authorObj?.optString("nickname")?.ifBlank { null }
                        ?: "unknown"
                    val title = d.optString("title").ifBlank { d.optString("desc") }
                    return@withContext Meta(id, author, title, play)
                }
            } catch (e: IOException) {
                last = e
            } catch (e: org.json.JSONException) {
                last = e
            }
            if (attempt < ATTEMPTS - 1) delay(1000L shl attempt)
        }
        throw IOException(last?.message ?: "Could not resolve link", last)
    }

    private companion object {
        const val API = "https://www.tikwm.com/api/"
        const val UA = "Mozilla/5.0"
        const val ATTEMPTS = 3
    }
}
