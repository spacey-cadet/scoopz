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

    /** Strips frame labels and chronology phrases the model sometimes leaks. */
    fun cleanCaption(raw: String): String {
        var out = raw
        out = out.replace(Regex("""\[(Beginning|Early-mid|Late-mid|End|Frame \d+)]\s*""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\b(in|by)\s+(frame|shot)\s*\d+\b""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\b(frame|shot)\s*\d+\b""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\b(by the end|starts calm, ends|in this clip)\b""", RegexOption.IGNORE_CASE), "")
        out = out.replace(Regex("""\s{2,}"""), " ")
        return out.trim(' ', ',', '.', '-')
    }
}
