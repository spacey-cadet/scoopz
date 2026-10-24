package com.scoopz.app.caption

object Hashtags {
    private val FALLBACK = listOf("#fyp", "#viral")
    private const val MAX_TAGS = 8

    /** Cleans model output into unique, lowercase, #-prefixed tags. */
    fun normalize(raw: List<String>): List<String> {
        val cleaned = raw.asSequence()
            .flatMap { it.split(' ', ',', '\n').asSequence() }
            .map { it.trim().trimStart('#').filter { c -> c.isLetterOrDigit() || c == '_' }.lowercase() }
            .filter { it.length in 2..30 }
            .map { "#$it" }
            .distinct()
            .take(MAX_TAGS)
            .toList()
        return cleaned.ifEmpty { FALLBACK }
    }

    /** Fallback when the model ignored the JSON format: grab #tags from free text. */
    fun extract(text: String): List<String> =
        Regex("""#\w{2,30}""").findAll(text).map { it.value }.toList()
}
