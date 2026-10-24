package com.scoopz.app.error

import java.io.InterruptedIOException
import java.net.ConnectException
import java.net.UnknownHostException
import javax.net.ssl.SSLException

enum class Stage { DOWNLOAD, CAPTION }

/** What the card can offer besides the Retry/Caption button that is always there. */
enum class Fix { NONE, OPEN_SETTINGS }

/** One row of the error matrix. */
data class ErrorInfo(
    val code: String,
    val title: String,
    val what: String,
    val fix: String,
    val action: Fix = Fix.NONE,
    /** False when an AI explanation can't add anything (or can't work, e.g. no key / no internet). */
    val aiUseful: Boolean = true,
)

/**
 * Error "middleware": every failure passes through [encode] where it is matched against the
 * matrix below. The card stores `CODE\nraw message`, and the UI turns that into a title,
 * an explanation and a fix. Unknown errors can still be explained by an NVIDIA model (see ErrorAi).
 */
object ErrorExplainer {

    private val NO_NETWORK = ErrorInfo(
        "E_NO_NETWORK", "No internet connection",
        "The phone couldn't reach the server.",
        "Check Wi-Fi or mobile data, then tap Retry.",
        aiUseful = false,
    )
    private val TIMEOUT = ErrorInfo(
        "E_TIMEOUT", "The server took too long",
        "The server didn't answer in time. The free NVIDIA servers get busy at times.",
        "Try again in a minute. If it keeps happening, pick a faster vision model or set a backup model in Settings.",
        Fix.OPEN_SETTINGS,
    )
    private val NO_KEY = ErrorInfo(
        "E_NO_KEY", "No NVIDIA key saved",
        "Captions need your free NVIDIA API key, and none is saved yet.",
        "Open Settings, paste the key from build.nvidia.com (Settings, API Keys), save, then tap Caption.",
        Fix.OPEN_SETTINGS, aiUseful = false,
    )
    private val BAD_KEY = ErrorInfo(
        "E_BAD_KEY", "NVIDIA rejected your key",
        "The key is wrong, expired or was deleted.",
        "Create a fresh key on build.nvidia.com and paste it in Settings.",
        Fix.OPEN_SETTINGS, aiUseful = false,
    )
    private val MODEL_403 = ErrorInfo(
        "E_MODEL_403", "Model not enabled for your key",
        "NVIDIA knows the model but your key isn't allowed to use it yet.",
        "Open the model's page on build.nvidia.com and tap Try API once, or choose a different model in Settings.",
        Fix.OPEN_SETTINGS,
    )
    private val MODEL_404 = ErrorInfo(
        "E_MODEL_404", "Model not available",
        "Your account can't use that model name. It may be retired or not offered to your account.",
        "Pick another vision model in Settings, or clear the backup model field.",
        Fix.OPEN_SETTINGS,
    )
    private val RATE = ErrorInfo(
        "E_RATE", "Too many requests",
        "The free tier allows about 40 requests a minute and that limit was hit.",
        "Wait a minute and tap Caption again. You can also lower Max requests per minute in Settings.",
        Fix.OPEN_SETTINGS,
    )
    private val SERVER = ErrorInfo(
        "E_SERVER", "NVIDIA had a server problem",
        "The server returned an error (HTTP 5xx). This is on their side, not yours.",
        "Try again in a few minutes.",
    )
    private val REJECTED = ErrorInfo(
        "E_REJECTED", "Request rejected by the model",
        "The model refused the request, often because it isn't a vision model or can't take images.",
        "Choose a vision model (one that accepts images) in Settings.",
        Fix.OPEN_SETTINGS,
    )
    private val BAD_REPLY = ErrorInfo(
        "E_BAD_REPLY", "Unusable answer from the model",
        "The model replied with something the app couldn't turn into a caption.",
        "Tap Redo. If it repeats, try another model.",
    )
    private val NO_FRAMES = ErrorInfo(
        "E_NO_FRAMES", "Can't read the video file",
        "The file may be damaged, still incomplete, or deleted from the phone.",
        "Delete this card and share the link again.",
        aiUseful = false,
    )
    private val TIKWM = ErrorInfo(
        "E_TIKWM", "Downloader service is down",
        "The service that turns TikTok links into video files isn't responding.",
        "Wait a bit and tap Retry.",
    )
    private val DL_EXPIRED = ErrorInfo(
        "E_DL_EXPIRED", "Download link expired or blocked",
        "The direct video link stopped working before it finished.",
        "Tap Retry. The app asks for a fresh link.",
    )
    private val STORAGE = ErrorInfo(
        "E_STORAGE", "Couldn't save the video",
        "The phone's storage is full or saving was refused.",
        "Free up some space, then tap Retry.",
        aiUseful = false,
    )
    private val LINK = ErrorInfo(
        "E_LINK", "This link couldn't be downloaded",
        "The video may be private, deleted, region locked, or not a normal TikTok video link.",
        "Open the link in TikTok to check it plays, then share it again.",
    )
    private val UNKNOWN = ErrorInfo(
        "E_UNKNOWN", "Something unexpected went wrong",
        "The app hit an error it doesn't have a specific explanation for.",
        "Tap Retry. Use Ask AI for an explanation, or Details to see the raw message.",
    )

    private val byCode: Map<String, ErrorInfo> =
        listOf(
            NO_NETWORK, TIMEOUT, NO_KEY, BAD_KEY, MODEL_403, MODEL_404, RATE, SERVER, REJECTED,
            BAD_REPLY, NO_FRAMES, TIKWM, DL_EXPIRED, STORAGE, LINK, UNKNOWN,
        ).associateBy { it.code }

    /** Called where a job fails. The result is what gets saved on the card. */
    fun encode(t: Throwable, stage: Stage): String {
        val raw = (t.message ?: t.javaClass.simpleName).take(500)
        return "${classify(t, raw, stage).code}\n$raw"
    }

    /** Called by the UI: turns a saved error back into (matrix row, raw message). */
    fun decode(stored: String?): Pair<ErrorInfo, String> {
        val s = stored.orEmpty()
        byCode[s.substringBefore('\n')]?.let { return it to s.substringAfter('\n', "") }
        return classify(null, s, null) to s // errors saved by older versions have no code
    }

    fun classify(t: Throwable?, raw: String, stage: Stage?): ErrorInfo {
        // When several models failed, each error is on its own line and the main model comes first:
        // explain that one. All lines remain visible under Details.
        val m = raw.lineSequence().firstOrNull { it.isNotBlank() }.orEmpty().lowercase()
        val isDownloadHttp = "download http" in m
        return when {
            t is UnknownHostException || t is ConnectException || t is SSLException ||
                "unable to resolve host" in m || "network problem" in m || "failed to connect" in m -> NO_NETWORK
            "add your nvidia api key" in m -> NO_KEY
            "http 401" in m -> BAD_KEY
            "tikwm http" in m -> TIKWM
            isDownloadHttp -> DL_EXPIRED
            "http 404" in m || "not available to your account" in m -> MODEL_404
            "http 403" in m -> MODEL_403
            "rate limited" in m || "http 429" in m -> RATE
            t is InterruptedIOException || "took too long" in m || "timeout" in m || "timed out" in m -> TIMEOUT
            Regex("""http 5\d\d""").containsMatchIn(m) -> SERVER
            "no space left" in m || "could not create media entry" in m || "cannot open output" in m -> STORAGE
            "could not read frames" in m -> NO_FRAMES
            "unreadable response" in m || "empty caption" in m -> BAD_REPLY
            "http 400" in m || "http 413" in m -> REJECTED
            stage == Stage.DOWNLOAD -> LINK
            else -> UNKNOWN
        }
    }
}
