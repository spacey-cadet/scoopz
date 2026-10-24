package com.scoopz.app.caption

import com.scoopz.app.data.Settings

/** POV and style-mode rotation, ported from newsbreak_captions.py. State survives restarts. */
object CaptionStyle {
    private val POV_MODES = listOf("third", "second", "first_observer")
    private val STYLE_MODES = listOf("drama_conflict", "mystery_curiosity", "outrage_opinion")

    data class Pick(val povLabel: String, val styleMode: String)

    /** Advances both rotations by one step and returns the next combination. */
    @Synchronized
    fun next(settings: Settings): Pick {
        val povIdx = (POV_MODES.indexOf(settings.povLast).takeIf { it >= 0 } ?: 0) + 1
        val pov = POV_MODES[povIdx % POV_MODES.size]
        settings.povLast = pov

        val styleIdx = settings.styleIndex
        settings.styleIndex = (styleIdx + 1) % STYLE_MODES.size
        return Pick(label(pov), STYLE_MODES[styleIdx % STYLE_MODES.size])
    }

    private fun label(mode: String) = when (mode) {
        "second" -> "SECOND PERSON"
        "first_observer" -> "FIRST PERSON (OBSERVER)"
        else -> "THIRD PERSON"
    }

    private val PREAMBLE = Regex("""^(here('s| is| are)|sure|certainly|okay|ok|of course)\b.*""", RegexOption.IGNORE_CASE)
    private val COMMENTARY_LINE = Regex(
        """^(this|the) (caption|post)\b.*|^(note|explanation|why)\b.*|^\(.*\)$""",
        RegexOption.IGNORE_CASE,
    )
    private val COMMENTARY_TAIL = Regex(
        """\s*\b(this|the) caption\b.*$""",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )

    /**
     * Turns raw model output into just the caption: drops intro lines ("Here is a caption..."),
     * trailing commentary ("This caption hooks..."), markdown, wrapping quotes, hashtags and
     * frame labels that leaked from the prompt.
     */
    fun cleanCaption(raw: String): String {
        var out = raw
        out = out.replace(Regex("""\[(Beginning|Early-mid|Late-mid|End|Frame \d+)]\s*""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\b(in|by)\s+(frame|shot)\s*\d+\b""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\b(frame|shot)\s*\d+\b""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\b(by the end|starts calm, ends|in this clip)\b""", RegexOption.IGNORE_CASE), "")

        out = out.lines()
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .filterNot { line ->
                (PREAMBLE.matches(line) && (line.endsWith(":") || line.contains("caption", ignoreCase = true))) ||
                    COMMENTARY_LINE.matches(line)
            }
            .joinToString(" ")

        out = COMMENTARY_TAIL.replace(out, "")
        out = out.replace("**", "").replace("__", "").replace("`", "").replace("*", "")
        out = out.replace(Regex("""#\w+"""), "") // hashtags belong in the hashtag field
        out = out.replace(Regex("""\s{2,}"""), " ").trim()
        out = stripWrappingQuotes(out)
        return out.trim(' ', ',', '.', '-')
    }

    private fun stripWrappingQuotes(s: String): String {
        if (s.length < 2) return s
        val close = when (s.first()) {
            '"' -> '"'
            '“' -> '”'
            '\'' -> '\''
            '‘' -> '’'
            else -> return s
        }
        if (s.last() != close) {
            // A lone opening double quote with no closing one is a leftover: drop it.
            val doubles = s.count { it == '"' || it == '“' || it == '”' }
            return if (doubles == 1 && (s.first() == '"' || s.first() == '“')) s.substring(1).trim() else s
        }
        val inner = s.substring(1, s.length - 1)
        return if (inner.none { it == s.first() || it == close }) inner.trim() else s
    }
}
