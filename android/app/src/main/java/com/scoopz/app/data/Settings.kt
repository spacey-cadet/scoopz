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

    var autoCaption: Boolean
        get() = prefs.getBoolean("auto_caption", true)
        set(v) = prefs.edit().putBoolean("auto_caption", v).apply()

    /** Free tier is ~40 requests/min per key across all models; stay under it. */
    var requestsPerMinute: Int
        get() = prefs.getInt("rpm", 30)
        set(v) = prefs.edit().putInt("rpm", v.coerceIn(1, 40)).apply()

    companion object {
        const val DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
        const val DEFAULT_MODEL = "meta/llama-3.2-11b-vision-instruct"
    }
}
