package com.scoopz.app.ui

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.scoopz.app.ScoopzApp
import com.scoopz.app.data.VideoEntity
import com.scoopz.app.error.AiState
import com.scoopz.app.error.Stage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import com.scoopz.app.util.LinkParser
import com.scoopz.app.work.Enqueue
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

class LibraryViewModel(private val app: Application) : AndroidViewModel(app) {
    private val g = ScoopzApp.graph
    private val dao = g.db.dao()

    val videos: StateFlow<List<VideoEntity>> = dao.observeAll()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    /** State of the "Ask AI" error explanation dialog (null = closed). */
    private val _ai = MutableStateFlow<AiState?>(null)
    val aiState: StateFlow<AiState?> = _ai
    private var aiJob: Job? = null

    fun explainWithAi(stage: Stage, raw: String) {
        aiJob?.cancel()
        _ai.value = AiState.Loading
        aiJob = viewModelScope.launch {
            _ai.value = try {
                AiState.Done(g.errorAi.explain(stage, raw))
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                AiState.Failed(e.message ?: "AI explanation isn't available right now.")
            }
        }
    }

    fun dismissAi() {
        aiJob?.cancel()
        _ai.value = null
    }

    /** Picker selection, capped at 3 like the original script. */
    private val _selected = MutableStateFlow<Set<String>>(emptySet())
    val selected: StateFlow<Set<String>> = _selected

    fun toggleSelected(url: String) = _selected.update {
        when {
            url in it -> it - url
            it.size < MAX_SELECTED -> it + url
            else -> it
        }
    }

    fun clearSelection() { _selected.value = emptySet() }

    /** Returns how many new links were queued. */
    fun addFromText(text: String?, onDone: (Int) -> Unit) {
        val links = LinkParser.extract(text)
        viewModelScope.launch {
            val n = links.count { Enqueue.link(app, it) }
            onDone(n)
        }
    }

    fun retry(url: String) = viewModelScope.launch { Enqueue.retry(app, url) }
    fun caption(url: String) = viewModelScope.launch { Enqueue.recaption(app, url) }

    /** Re-caption using the slower vision model (looks at the actual frame). */
    fun captionVision(url: String) = viewModelScope.launch { Enqueue.recaption(app, url, vision = true) }

    fun captionSelected() {
        val urls = _selected.value
        urls.forEach { caption(it) }
    }

    fun delete(v: VideoEntity) = viewModelScope.launch {
        v.mediaUri?.let { runCatching { app.contentResolver.delete(Uri.parse(it), null, null) } }
        dao.delete(v.sourceUrl)
        _selected.update { it - v.sourceUrl }
    }

    private companion object { const val MAX_SELECTED = 3 }
}
