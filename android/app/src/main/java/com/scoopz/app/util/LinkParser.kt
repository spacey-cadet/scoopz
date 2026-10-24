package com.scoopz.app.util

object LinkParser {
    private val TIKTOK = Regex("""https?://(?:(?:www|vt|vm|m)\.)?tiktok\.com/\S+""", RegexOption.IGNORE_CASE)

    /** Pulls every TikTok link out of arbitrary shared text, trimmed of trailing punctuation. */
    fun extract(text: String?): List<String> {
        if (text.isNullOrBlank()) return emptyList()
        return TIKTOK.findAll(text)
            .map { it.value.trimEnd('.', ',', ')', ']', '!', '?', '"', '\'') }
            .distinct()
            .toList()
    }
}
