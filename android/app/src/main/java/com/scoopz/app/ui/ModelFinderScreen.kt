package com.scoopz.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.scoopz.app.models.Probe

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun ModelFinderScreen(onBack: () -> Unit) {
    val vm: ModelFinderViewModel = viewModel()
    val ui by vm.ui.collectAsState()
    var custom by remember { mutableStateOf("") }
    BackHandler(onBack = onBack)

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("Find models") },
            navigationIcon = { TextButton(onClick = onBack) { Text("Back") } },
        )
    }) { pad ->
        Column(
            Modifier.fillMaxSize().padding(pad).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                "Tests vision models with a tiny picture and text models with a one-word question, and times each answer. " +
                    "It uses a few of your free requests. Text models write the fast captions; vision models are the slower option.",
                style = MaterialTheme.typography.bodySmall,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = vm::start, enabled = !ui.running) {
                    Text(if (ui.rows.isEmpty()) "Find models" else "Test again")
                }
                if (ui.running) TextButton(onClick = vm::stop) { Text("Stop") }
                if (!ui.running && (ui.working.isNotEmpty() || ui.workingText.isNotEmpty())) {
                    TextButton(onClick = vm::useFastest) { Text("Use fastest") }
                }
            }
            ui.note?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            ui.measuredAt?.let {
                Text("Measured at $it. Speeds change through the day.", style = MaterialTheme.typography.bodySmall)
            }
            OutlinedTextField(
                custom, { custom = it }, Modifier.fillMaxWidth(),
                label = { Text("Test a model name") }, singleLine = true,
            )
            FlowRow {
                TextButton(onClick = { vm.addCustom(custom, Kind.VISION); custom = "" }, enabled = !ui.running && custom.isNotBlank()) {
                    Text("Test as vision")
                }
                TextButton(onClick = { vm.addCustom(custom, Kind.TEXT); custom = "" }, enabled = !ui.running && custom.isNotBlank()) {
                    Text("Test as text")
                }
            }

            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(
                    ui.rows.sortedWith(compareBy({ it.kind }, { order(it.probe) }, { elapsed(it.probe) })),
                    key = { it.key },
                ) { row ->
                    ModelCard(
                        row = row,
                        isMain = row.kind == Kind.VISION && row.model == ui.main,
                        isBackup = row.kind == Kind.VISION && row.model == ui.backup,
                        isText = row.kind == Kind.TEXT && row.model == ui.textModel,
                        onMain = { vm.setMain(row.model) },
                        onBackup = { vm.setBackup(row.model) },
                        onText = { vm.setText(row.model) },
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ModelCard(
    row: ModelRow,
    isMain: Boolean,
    isBackup: Boolean,
    isText: Boolean,
    onMain: () -> Unit,
    onBackup: () -> Unit,
    onText: () -> Unit,
) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    row.model, Modifier.weight(1f),
                    style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
                Text(
                    if (row.kind == Kind.VISION) "VISION" else "TEXT",
                    style = MaterialTheme.typography.labelSmall,
                )
                if (isMain) Text("MAIN", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelSmall)
                if (isBackup) Text("BACKUP", color = MaterialTheme.colorScheme.tertiary, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelSmall)
                if (isText) Text("IN USE", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelSmall)
            }
            when (val p = row.probe) {
                Probe.Pending -> Text("Waiting...", style = MaterialTheme.typography.bodySmall)
                Probe.Testing -> {
                    Text("Testing...", style = MaterialTheme.typography.bodySmall)
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                }
                is Probe.Ok -> {
                    Text("Works. Answered in ${seconds(p.ms)}", style = MaterialTheme.typography.bodyMedium)
                    FlowRow {
                        if (row.kind == Kind.VISION) {
                            TextButton(onClick = onMain, enabled = !isMain) { Text("Set as main") }
                            TextButton(onClick = onBackup, enabled = !isBackup) { Text("Set as backup") }
                        } else {
                            TextButton(onClick = onText, enabled = !isText) { Text("Use for fast captions") }
                        }
                    }
                }
                is Probe.Wrong -> Text(
                    "Answered in ${seconds(p.ms)} but didn't see the picture (said \"${p.answer}\"). Not suitable.",
                    style = MaterialTheme.typography.bodySmall,
                )
                is Probe.Failed -> Text(p.reason, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

private fun seconds(ms: Long) = "%.1f s".format(ms / 1000.0)

private fun order(p: Probe): Int = when (p) {
    is Probe.Ok -> 0
    Probe.Testing -> 1
    Probe.Pending -> 2
    is Probe.Wrong -> 3
    is Probe.Failed -> 4
}

private fun elapsed(p: Probe): Long = when (p) {
    is Probe.Ok -> p.ms
    is Probe.Wrong -> p.ms
    else -> 0L
}
