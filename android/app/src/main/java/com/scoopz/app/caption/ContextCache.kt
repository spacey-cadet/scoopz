package com.scoopz.app.caption

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * Remembers what was worked out on the phone for a video (on-screen text, labels, transcript)
 * so Redo only has to call the text model: no frame scan, no OCR and no second transcription.
 * Plain files, so the database doesn't change.
 */
object ContextCache {
    /** [transcript] null = never attempted; "" = attempted, nothing to transcribe. */
    data class Info(val onScreenText: String, val labels: List<String>, val transcript: String?)

    private fun file(context: Context, url: String): File {
        val dir = File(context.filesDir, "ctx").apply { mkdirs() }
        val name = MessageDigest.getInstance("SHA-1").digest(url.toByteArray()).joinToString("") { "%02x".format(it) }
        return File(dir, "$name.json")
    }

    fun read(context: Context, url: String): Info? = runCatching {
        val j = JSONObject(file(context, url).readText())
        val arr = j.optJSONArray("labels") ?: JSONArray()
        Info(
            onScreenText = j.optString("onScreen"),
            labels = List(arr.length()) { arr.getString(it) },
            transcript = if (j.has("transcript")) j.getString("transcript") else null,
        )
    }.getOrNull()

    fun write(context: Context, url: String, info: Info) {
        runCatching {
            val j = JSONObject()
                .put("onScreen", info.onScreenText)
                .put("labels", JSONArray(info.labels))
            info.transcript?.let { j.put("transcript", it) }
            file(context, url).writeText(j.toString())
        }
    }

    fun delete(context: Context, url: String) {
        runCatching { file(context, url).delete() }
    }
}
