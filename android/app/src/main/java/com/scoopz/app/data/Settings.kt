package com.scoopz.app.data

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** Encrypted settings. The NVIDIA API key never touches plain-text storage. */
class Settings(context: Context) {
    private val prefs = EncryptedSharedPreferences.create(
        context,
        "scoopz_secure",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var apiKey: String
        get() = prefs.getString("api_key", "") ?: ""
        set(v) = prefs.edit().putString("api_key", v.trim()).apply()

    var baseUrl: String
        get() = prefs.getString("base_url", DEFAULT_BASE_URL) ?: DEFAULT_BASE_URL
        set(v) = prefs.edit().putString("base_url", v.trim().trimEnd('/')).apply()

    var model: String
        get() = prefs.getString("model", DEFAULT_MODEL) ?: DEFAULT_MODEL
        set(v) = prefs.edit().putString("model", v.trim()).apply()

    /** Tried automatically when the main model times out or fails. Leave blank to disable. */
    var backupModel: String
        get() {
            val v = prefs.getString("backup_model", DEFAULT_BACKUP_MODEL) ?: DEFAULT_BACKUP_MODEL
            // Earlier builds defaulted to a model many accounts cannot use (HTTP 404): treat it as off.
            return if (v == RETIRED_BACKUP_MODEL) DEFAULT_BACKUP_MODEL else v
        }
        set(v) = prefs.edit().putString("backup_model", v.trim()).apply()

    /** Fast text-only model: writes captions from the title, on-screen text and image labels (no image sent). */
    var textModel: String
        get() = prefs.getString("text_model", DEFAULT_TEXT_MODEL) ?: DEFAULT_TEXT_MODEL
        set(v) = prefs.edit().putString("text_model", v.trim()).apply()

    /** On = always use the vision model (slower). Off = text first, vision only when info is thin or asked for. */
    var visionAlways: Boolean
        get() = prefs.getBoolean("vision_always", false)
        set(v) = prefs.edit().putBoolean("vision_always", v).apply()

    /** Off = send only the single best frame (fastest). On = best two frames, slower. */
    var twoFrames: Boolean
        get() = prefs.getBoolean("two_frames", false)
        set(v) = prefs.edit().putBoolean("two_frames", v).apply()

    var autoCaption: Boolean
        get() = prefs.getBoolean("auto_caption", true)
        set(v) = prefs.edit().putBoolean("auto_caption", v).apply()

    /** Free tier is ~40 requests/min per key across all models; stay under it. */
    var requestsPerMinute: Int
        get() = prefs.getInt("rpm", 30)
        set(v) = prefs.edit().putInt("rpm", v.coerceIn(1, 40)).apply()

    /** Rotation state for caption POV / style (persisted like ~/.cache/scoopz_pov.json). */
    var povLast: String
        get() = prefs.getString("pov_last", "third") ?: "third"
        set(v) = prefs.edit().putString("pov_last", v).apply()

    var styleIndex: Int
        get() = prefs.getInt("style_idx", 0)
        set(v) = prefs.edit().putInt("style_idx", v).apply()

    companion object {
        const val DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
        const val DEFAULT_MODEL = "meta/llama-3.2-11b-vision-instruct"
        const val DEFAULT_BACKUP_MODEL = "" // off until you choose a model that works on your key
        const val DEFAULT_TEXT_MODEL = "meta/llama-3.1-8b-instruct" // a guess: confirm it with Find models
        private const val RETIRED_BACKUP_MODEL = "microsoft/phi-3-vision-128k-instruct"
    }
}
