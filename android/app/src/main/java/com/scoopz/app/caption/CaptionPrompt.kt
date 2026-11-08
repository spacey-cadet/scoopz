package com.scoopz.app.caption

/** Prompt ported from CAPTION_PROMPT in newsbreak_captions.py, adapted to TikTok info plus 1-2 key frames. */
object CaptionPrompt {

    /** Vision prompt: the model also receives the picked frame(s). */
    fun build(
        pov: String,
        styleMode: String,
        title: String?,
        author: String?,
        onScreenText: String,
        transcript: String = "",
    ): String = fill(TEMPLATE, pov, styleMode, title, author, onScreenText, emptyList(), transcript)

    /** Text prompt: no image is sent, so the model works only from the info and on-device labels. */
    fun buildText(
        pov: String,
        styleMode: String,
        title: String?,
        author: String?,
        onScreenText: String,
        labels: List<String>,
        transcript: String,
    ): String {
        val textTemplate = TEMPLATE
            .replace(
                "The image shows 1-2 key frames from it (side by side, in time order if two).",
                "You cannot see the video: work only from the info below.",
            )
            .replace(
                "- The frames show the action, reaction and mood.",
                "- The visible-things labels are generic hints about what is on screen. Never invent details beyond them.",
            )
            .replace(
                "CONTEXT: Use the frames only to infer tension/reveals; never mention frames or their order.",
                "CONTEXT: Never mention that you only have text, labels or frames.",
            )
        return fill(textTemplate, pov, styleMode, title, author, onScreenText, labels, transcript)
    }

    /** One video inside a batch request. Each keeps its own POV and style so the rotation still varies. */
    data class BatchEntry(
        val pov: String,
        val styleMode: String,
        val title: String?,
        val author: String?,
        val onScreenText: String,
        val labels: List<String>,
        val transcript: String,
    )

    /**
     * Text prompt for several videos at once (one API request instead of one per video).
     * The shared rules are sent once; every video gets its own numbered block and the reply must
     * come back as numbered blocks so each caption can be matched to its video.
     */
    fun buildBatch(entries: List<BatchEntry>): String {
        val videos = entries.mapIndexed { i, e ->
            "### VIDEO ${i + 1}\nPOV: ${e.pov}\nSTYLE MODE: ${e.styleMode}\nTIKTOK INFO:\n" +
                info(e.title, e.author, e.onScreenText, e.labels, e.transcript)
        }.joinToString("\n\n")
        return BATCH_HEAD.replace("{n}", entries.size.toString()) +
            "\n\n" + videos +
            "\n\n" + RULES +
            "\n\n" + BATCH_OUTPUT.replace("{n}", entries.size.toString())
    }

    private fun info(
        title: String?,
        author: String?,
        onScreenText: String,
        labels: List<String>,
        transcript: String,
    ): String = buildString {
        if (!author.isNullOrBlank()) append("- Uploader: @${author.trim()}\n")
        if (!title.isNullOrBlank()) append("- Title/hashtags: ${title.trim().take(300)}\n")
        if (onScreenText.isNotBlank()) append("- Text visible in the video: ${onScreenText.take(300)}\n")
        if (labels.isNotEmpty()) append("- Visible things (generic on-device labels): ${labels.joinToString(", ")}\n")
        if (transcript.isNotBlank()) append("- Spoken words (transcript): ${transcript.take(600)}\n")
    }.ifBlank { "- (none available)\n" }.trimEnd()

    private fun fill(
        template: String,
        pov: String,
        styleMode: String,
        title: String?,
        author: String?,
        onScreenText: String,
        labels: List<String>,
        transcript: String,
    ): String = template
        .replace("{pov_mode}", pov)
        .replace("{style_mode}", styleMode)
        .replace("{tiktok_info}", info(title, author, onScreenText, labels, transcript))

    /** Everything after the per-video info: identical for the single and batch prompts. */
    private const val RULES = """GOAL:
- Sound crazy, or out of pocket
- Ground it in ONE specific detail from THIS video — an exact action, quote, object, or reaction visible or heard.
- Make it universally relatable — a judgment, question, or dare that anyone scrolling can instantly react to.
- It should feel like something a real person texted their friend, not a content writer.

REQUIREMENTS:
- No more than 100 characters total
- 1 sentence.
- 2-3 emojis total (distributed naturally, never clustered at the end)
- Must hook instantly (conflict, accusation, shock, stakes, or challenge) and escalate tension and force a reaction (question, judgment, or dare)
- Include at least one specific anchor detail (exact phrase/object/action) that clearly ties caption to this video
- Strongly evoke at least one: outrage, suspicion, disbelief, urgency, or moral tension
- Use pattern interruption — reframe the obvious in an unexpected way (see examples below)
- Include any names that appear in the TikTok info or visible text (for example "Jay Z talks about fame") for maximum specificity and searchability.
- Do NOT narrate calmly or describe scenery like a recap
- Do NOT repeat the TikTok title word for word: rewrite it with a hook

PATTERN INTERRUPTION EXAMPLES:
- Instead of "This dad is amazing" → "Nobody warned him about the tiny dictator 👑"
- Instead of "She caught him lying" → "He really thought the smile would save him 💀"
- Instead of "These dogs are funny" → "These dogs have a better commute than me 😭"
- Instead of "He's manifesting wealth" → "Manifesting a BMW while his knees file for divorce"
Use this energy — subvert the expected read, make the viewer feel slightly caught off guard.

STYLE:
- Raw, human, slightly uncomfortable
- Not formal, not news-like
- Avoid generic placeholders ("this guy", "some dude", "a woman")
- Use concrete high-stakes language, not neutral observation
- Warm confrontation for family/kids content — provocative but never mean
- Skeptical/sarcastic for finance or hustle content — not angry, just knowing
- Full chaos energy for animal or accident content — lean into absurdity
- Can include relatable/funny/sarcastic/chaotic edge when it increases shareability

MODE RULES:
- drama_conflict:
    DO: direct clash, accusation, betrayal, confrontation, power imbalance
    DON'T: vague tension ("something went wrong"), soft drama ("they disagreed")
    TONE: sharp, decisive, takes a side

- mystery_curiosity:
    DO: unanswered question, hidden detail, suspicious mismatch, "why is nobody talking about"
    DON'T: open-ended confusion that goes nowhere ("what even is this?")
    TONE: conspiratorial, slightly unsettled, pulls the viewer in

- outrage_opinion:
    DO: hot take, moral judgment, accountability, "pick a side" framing
    DON'T: both-sides neutrality, calm observation, fence-sitting
    TONE: confident, slightly provocative, demands a response

AVOID THESE PHRASES:
- "by frame 4", "in frame 1", "frame 2", "this shot"
- "starts calm, ends...", "by the end", "in this clip"
- Soft scenic lead-ins like "morning mist..." unless tied to immediate tension
- "this is wild", "you won't believe", "wait for it" — overused, skip them

HASHTAGS:
- Generate 7 relevant hashtags for this video. Avoid platform tags (no #tiktok, #instagram, etc.).
- Mix reaction tags (emotion/curiosity/energy) with content-specific tags tied to this video's topic."""

    private const val TEMPLATE = """Write a HIGH-CONVERSION caption for a short video. The image shows 1-2 key frames from it (side by side, in time order if two).

POV: {pov_mode}

TIKTOK INFO (the most reliable source for names, topics and facts):
{tiktok_info}

SIGNAL PRIORITY:
- Names, topics and claims come from the TikTok info above, including hashtags and visible text.
- The frames show the action, reaction and mood.
- NEVER identify a person from their face. Only use a person's name if the text above gives it.

STYLE MODE: {style_mode}

CONTEXT: Use the frames only to infer tension/reveals; never mention frames or their order.

""" + RULES + """

OUTPUT FORMAT (reply with ONLY this: no intro line, no explanation, no markdown, no quotes around the caption, no hashtags inside the caption):
CAPTION:
[your caption here]

HASHTAGS:
[hashtags here]"""

    private const val BATCH_HEAD = """Write a HIGH-CONVERSION caption for EACH of the {n} short videos listed below. You cannot see the videos: work only from the info given for each one.

Treat every video separately. NEVER mix details, names, topics or hashtags between videos. Each video has its own POV and STYLE MODE: follow them for that video only.

SIGNAL PRIORITY:
- Names, topics and claims come from each video's TIKTOK INFO, including hashtags and visible text. It is the most reliable source.
- The visible-things labels are generic hints about what is on screen. Never invent details beyond them.
- NEVER identify a person from their face. Only use a person's name if the text gives it.
- Never mention that you only have text or labels.

VIDEOS:"""

    private const val BATCH_OUTPUT = """OUTPUT FORMAT: reply with ONLY {n} blocks, one per video, in order, exactly like this (no intro line, no explanation, no markdown, no quotes around captions, no hashtags inside captions). Every block starts with its own header line "### VIDEO <number>" on a line by itself:

### VIDEO 1
CAPTION:
[caption for video 1]

HASHTAGS:
[hashtags for video 1]

### VIDEO 2
CAPTION:
[caption for video 2]

HASHTAGS:
[hashtags for video 2]

(and so on, up to VIDEO {n})"""
}
