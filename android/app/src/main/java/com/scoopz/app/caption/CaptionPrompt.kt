package com.scoopz.app.caption

/** Prompt ported from CAPTION_PROMPT in newsbreak_captions.py, adapted to one stitched image. */
object CaptionPrompt {

    fun build(pov: String, styleMode: String, hasSpeech: Boolean, transcript: String): String {
        // Without usable speech the original script shifts weight to the visuals.
        val audioW = if (hasSpeech) 0.60 else 0.20
        val frameW = if (hasSpeech) 0.40 else 0.80
        val base = TEMPLATE
            .replace("{pov_mode}", pov)
            .replace("{style_mode}", styleMode)
            .replace("{audio_weight}", "%.2f".format(audioW))
            .replace("{frame_weight}", "%.2f".format(frameW))
        return if (hasSpeech) {
            "Transcript (prioritize if speech is meaningful): ${transcript.take(1500)}\n\n$base"
        } else base
    }

    private const val TEMPLATE = """Write a HIGH-CONVERSION caption based on the image: it stitches 3 frames from one video side by side (beginning, middle, end, left to right).

POV: {pov_mode}

SIGNAL WEIGHTS:
- Audio/transcript weight: {audio_weight}
- Visual frames weight: {frame_weight}
- Default: prioritize spoken words, claims, arguments, admissions, and key quotes from transcript when provided.
- If there is no transcript, rely on the visuals.

STYLE MODE: {style_mode}

CONTEXT: Use the progression across the frames only to infer tension/reveals; never mention frame order explicitly.

GOAL:
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
- Ensure any names mentioned in the transcript or visible in frames are included in the caption for maximum specificity and searchability.
- Do NOT narrate calmly or describe scenery like a recap

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
- Mix reaction tags (emotion/curiosity/energy) with content-specific tags tied to this video's topic.

OUTPUT FORMAT:
CAPTION:
[your caption here]

HASHTAGS:
[hashtags here]"""
}
