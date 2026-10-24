package com.scoopz.app.caption

/**
 * Port of normalize_hashtags() from newsbreak_captions.py.
 * The model only suggests loose tags; this layer owns the final mix:
 * 4 mandatory general tags + up to 3 video-specific tags (max 7).
 */
object Hashtags {
    private val PLATFORM = setOf(
        "tiktok", "instagram", "reels", "youtube", "shorts", "facebook", "snapchat",
        "fyp", "foryou", "foryoupage", "viral", // generic reach tags: not about this video
    )

    private val GENERAL_VIRAL_POOL = listOf(
        "wtfmoment", "awkward", "watch", "reaction", "relatable", "breakingnews", "local",
        "funny", "messy", "unreal", "mindblown", "chaosmode", "unexpected", "outofpocket", "wildenergy",
    )
    private val MANDATORY = listOf("scoopz", "funny", "trending", "scoopzfirst")
    private val GENERAL_HINT_TOKENS = listOf("wtf", "wild", "crazy", "awkward", "watch", "reaction", "chaos", "messy", "relatable")

    private val KEYWORD_TAGS = listOf(
        "court" to "courtdrama", "judge" to "courtdrama",
        "police" to "policeinvestigation", "officer" to "policeinvestigation",
        "press" to "pressconference", "conference" to "pressconference",
        "public" to "publicreaction",
        "argument" to "heatedargument", "fight" to "heatedargument",
        "confront" to "confrontation", "accuse" to "accusation",
        "scam" to "scamalert", "fraud" to "scamalert",
        "arrest" to "arrestupdate", "lawyer" to "legaldrama",
    )

    private val STOPWORDS = setOf(
        "the", "and", "for", "that", "with", "this", "from", "your", "you", "they",
        "have", "just", "what", "when", "were", "been", "about", "into", "over", "then",
        "them", "their", "there", "would", "could", "should", "video", "caption",
        "thinks", "think", "says", "said", "watching", "watch", "being", "does", "will", "like",
        "make", "made", "know", "gets", "getting", "after", "before", "every", "still", "really",
        "going", "these", "those", "where", "which", "while", "other", "than", "also", "only",
        "very", "much", "more", "most", "some", "such", "kids", "cringe", "reveals", "talks",
    )
    private val TYPOS = mapOf("localanews" to "localnews")
    private val BREAKING = listOf(
        "breaking", "just in", "developing", "urgent", "right now", "live now", "confirmed", "official statement",
    )

    private val TAG_RE = Regex("#([a-z0-9_]+)")
    private val WORD_RE = Regex("[a-z]{4,}")

    fun normalize(hashtagsText: String, caption: String, transcript: String = ""): String {
        val extracted = LinkedHashSet<String>()
        for (m in TAG_RE.findAll(hashtagsText.lowercase())) {
            val tag = TYPOS[m.groupValues[1]] ?: m.groupValues[1]
            if (tag.isNotEmpty() && tag !in PLATFORM) extracted += tag
        }

        val videoSpecific = extracted.filterNot(::isGeneralViral).toMutableList()
        if (videoSpecific.isEmpty()) {
            deriveVideoSpecific(caption, transcript).firstOrNull()?.let { videoSpecific += it }
        }
        if (videoSpecific.isEmpty() && looksBreaking(caption, transcript)) videoSpecific += "breakingnews"

        val final = LinkedHashSet<String>()
        (MANDATORY + videoSpecific.take(3)).forEach { if (it.isNotEmpty() && it !in PLATFORM) final += it }

        if (final.none { !isGeneralViral(it) && it !in MANDATORY }) {
            val fallback = videoSpecific.firstOrNull() ?: "localnews"
            if (fallback !in PLATFORM) final += fallback
        }
        return final.take(7).joinToString(" ") { "#$it" }
    }

    private fun isGeneralViral(tag: String) =
        tag in GENERAL_VIRAL_POOL || GENERAL_HINT_TOKENS.any { it in tag }

    private fun looksBreaking(caption: String, transcript: String): Boolean {
        val text = "$caption $transcript".lowercase()
        return BREAKING.any { it in text }
    }

    private fun deriveVideoSpecific(caption: String, transcript: String): List<String> {
        val text = "$caption $transcript".lowercase()
        val tags = mutableListOf<String>()
        for ((key, tag) in KEYWORD_TAGS) if (key in text && tag !in tags) tags += tag

        if (tags.size < 3) {
            for (m in WORD_RE.findAll(text)) {
                val w = m.value
                if (w in STOPWORDS || GENERAL_VIRAL_POOL.any { w in it }) continue
                if (w !in tags) tags += w
                if (tags.size >= 3) break
            }
        }
        return tags.take(3)
    }
}
