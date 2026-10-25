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

data class ModelRow(val model: String, val probe: Probe)

data class FinderUi(
    val running: Boolean = false,
    val note: String? = null,
    val rows: List<ModelRow> = emptyList(),
    val main: String = "",
    val backup: String = "",
    val measuredAt: String? = null,
) {
    val working: List<ModelRow> get() = rows.filter { it.probe is Probe.Ok }.sortedBy { (it.probe as Probe.Ok).ms }
}

class ModelFinderViewModel(app: Application) : AndroidViewModel(app) {
    private val g = ScoopzApp.graph
    private val _ui = MutableStateFlow(FinderUi(main = g.settings.model, backup = g.settings.backupModel))
    val ui: StateFlow<FinderUi> = _ui
    private var job: Job? = null

    /** Lists the models the key can see, keeps the vision-looking ones, and times each. */
    fun start() {
        if (_ui.value.running) return
        if (g.settings.apiKey.isBlank()) {
            _ui.update { it.copy(note = "Add your NVIDIA API key in Settings first.") }
            return
        }
        job = viewModelScope.launch {
            _ui.update { it.copy(running = true, note = "Asking NVIDIA which models your key can use...", rows = emptyList(), measuredAt = null) }

            var note: String? = null
            val listed = try {
                g.modelFinder.listModels()
            } catch (e: AuthFailed) {
                _ui.update { it.copy(running = false, note = e.message) }
                return@launch
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                note = "Couldn't read NVIDIA's model list (${e.message}). Testing a built-in list of guesses instead."
                ModelFinder.SEED
            }

            // Always include the models currently in Settings so you can see how they compare.
            val names = (listOf(g.settings.model, g.settings.backupModel).filter { it.isNotBlank() } +
                g.modelFinder.candidates(listed)).distinct()
            if (names.isEmpty()) {
                _ui.update { it.copy(running = false, note = "No likely vision models found. Type a model name below to test it.") }
                return@launch
            }
            _ui.update { it.copy(note = note) }
            testNames(names, clear = true)
        }
    }

    /** Tests one model name typed by the user. */
    fun addCustom(name: String) {
        val n = name.trim()
        if (n.isEmpty() || _ui.value.running) return
        if (g.settings.apiKey.isBlank()) {
            _ui.update { it.copy(note = "Add your NVIDIA API key in Settings first.") }
            return
        }
        job = viewModelScope.launch { testNames(listOf(n), clear = false) }
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

    fun clearBackup() = setBackup("")

    /** Fastest working model becomes main, the next fastest becomes backup. */
    fun useFastest() {
        val fastest = _ui.value.working
        fastest.getOrNull(0)?.let { setMain(it.model) }
        fastest.getOrNull(1)?.let { setBackup(it.model) }
    }

    private suspend fun testNames(names: List<String>, clear: Boolean) {
        _ui.update { u ->
            val kept = if (clear) emptyList() else u.rows.filterNot { it.model in names }
            u.copy(running = true, rows = kept + names.map { ModelRow(it, Probe.Pending) })
        }
        try {
            coroutineScope {
                val gate = Semaphore(2) // two at a time, also spaced by the shared rate limiter
                names.map { m ->
                    async {
                        gate.withPermit {
                            setProbe(m, Probe.Testing)
                            setProbe(m, g.modelFinder.probe(m))
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

    private fun setProbe(model: String, probe: Probe) {
        _ui.update { u -> u.copy(rows = u.rows.map { if (it.model == model) it.copy(probe = probe) else it }) }
    }
}
