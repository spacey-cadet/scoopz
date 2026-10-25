package com.scoopz.app.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.scoopz.app.ScoopzApp
import com.scoopz.app.models.AuthFailed
import com.scoopz.app.models.ModelFinder
import com.scoopz.app.models.Probe
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import java.time.LocalTime

enum class Kind { VISION, TEXT }

data class ModelRow(val model: String, val probe: Probe, val kind: Kind = Kind.VISION) {
    val key: String get() = "${kind.name}:$model"
}

data class FinderUi(
    val running: Boolean = false,
    val note: String? = null,
    val rows: List<ModelRow> = emptyList(),
    val main: String = "",
    val backup: String = "",
    val textModel: String = "",
    val measuredAt: String? = null,
) {
    val working: List<ModelRow>
        get() = rows.filter { it.kind == Kind.VISION && it.probe is Probe.Ok }.sortedBy { (it.probe as Probe.Ok).ms }
    val workingText: List<ModelRow>
        get() = rows.filter { it.kind == Kind.TEXT && it.probe is Probe.Ok }.sortedBy { (it.probe as Probe.Ok).ms }
}

class ModelFinderViewModel(app: Application) : AndroidViewModel(app) {
    private val g = ScoopzApp.graph
    private val _ui = MutableStateFlow(
        FinderUi(main = g.settings.model, backup = g.settings.backupModel, textModel = g.settings.textModel)
    )
    val ui: StateFlow<FinderUi> = _ui
    private var job: Job? = null

    /** Lists the models the key can see, keeps likely vision and text models, and times each. */
    fun start() {
        if (_ui.value.running) return
        if (g.settings.apiKey.isBlank()) {
            _ui.update { it.copy(note = "Add your NVIDIA API key in Settings first.") }
            return
        }
        job = viewModelScope.launch {
            _ui.update { it.copy(running = true, note = "Asking NVIDIA which models your key can use...", rows = emptyList(), measuredAt = null) }

            var note: String? = null
            var visionList: List<String> = emptyList()
            var textList: List<String> = emptyList()
            try {
                val listed = g.modelFinder.listModels()
                visionList = g.modelFinder.candidates(listed)
                textList = g.modelFinder.textCandidates(listed)
            } catch (e: AuthFailed) {
                _ui.update { it.copy(running = false, note = e.message) }
                return@launch
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                note = "Couldn't read NVIDIA's model list (${e.message}). Testing a built-in list of guesses instead."
                visionList = ModelFinder.SEED
                textList = ModelFinder.TEXT_SEED
            }

            // Always include the models currently in Settings so you can see how they compare.
            val s = g.settings
            val vision = (listOf(s.model, s.backupModel).filter { it.isNotBlank() } + visionList).distinct()
            val text = (listOf(s.textModel).filter { it.isNotBlank() } + textList).distinct()
            val items = vision.map { it to Kind.VISION } + text.map { it to Kind.TEXT }
            if (items.isEmpty()) {
                _ui.update { it.copy(running = false, note = "No likely models found. Type a model name below to test it.") }
                return@launch
            }
            _ui.update { it.copy(note = note) }
            testItems(items, clear = true)
        }
    }

    /** Tests one model name typed by the user, as a vision or a text model. */
    fun addCustom(name: String, kind: Kind) {
        val n = name.trim()
        if (n.isEmpty() || _ui.value.running) return
        if (g.settings.apiKey.isBlank()) {
            _ui.update { it.copy(note = "Add your NVIDIA API key in Settings first.") }
            return
        }
        job = viewModelScope.launch { testItems(listOf(n to kind), clear = false) }
    }

    fun stop() {
        job?.cancel()
    }

    fun setMain(model: String) {
        g.settings.model = model
        _ui.update { it.copy(main = g.settings.model) }
    }

    fun setBackup(model: String) {
        g.settings.backupModel = model
        _ui.update { it.copy(backup = g.settings.backupModel) }
    }

    fun setText(model: String) {
        g.settings.textModel = model
        _ui.update { it.copy(textModel = g.settings.textModel) }
    }

    /** Fastest working vision model becomes main, the next becomes backup, and the fastest text model is used for text. */
    fun useFastest() {
        val vision = _ui.value.working
        vision.getOrNull(0)?.let { setMain(it.model) }
        vision.getOrNull(1)?.let { setBackup(it.model) }
        _ui.value.workingText.getOrNull(0)?.let { setText(it.model) }
    }

    private suspend fun testItems(items: List<Pair<String, Kind>>, clear: Boolean) {
        _ui.update { u ->
            val kept = if (clear) emptyList() else u.rows.filterNot { r -> items.any { it.first == r.model && it.second == r.kind } }
            u.copy(running = true, rows = kept + items.map { (m, k) -> ModelRow(m, Probe.Pending, k) })
        }
        try {
            coroutineScope {
                val gate = Semaphore(2) // two at a time, also spaced by the shared rate limiter
                items.map { (m, k) ->
                    async {
                        gate.withPermit {
                            setProbe(m, k, Probe.Testing)
                            setProbe(m, k, g.modelFinder.probe(m, textOnly = k == Kind.TEXT))
                        }
                    }
                }.awaitAll()
            }
            _ui.update { it.copy(measuredAt = LocalTime.now().withSecond(0).withNano(0).toString()) }
        } catch (e: AuthFailed) {
            _ui.update { it.copy(note = e.message) }
        } finally {
            _ui.update { u ->
                u.copy(
                    running = false,
                    rows = u.rows.map { if (it.probe == Probe.Testing) it.copy(probe = Probe.Pending) else it },
                )
            }
        }
    }

    private fun setProbe(model: String, kind: Kind, probe: Probe) {
        _ui.update { u -> u.copy(rows = u.rows.map { if (it.model == model && it.kind == kind) it.copy(probe = probe) else it }) }
    }
}
