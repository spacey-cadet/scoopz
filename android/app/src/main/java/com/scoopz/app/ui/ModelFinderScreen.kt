package com.scoopz.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
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

@OptIn(ExperimentalMaterial3Api::class)
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
                "Sends each model that might accept images a tiny test picture and times its answer. " +
                    "It uses a few of your free requests. Only models that answer correctly are offered.",
                style = MaterialTheme.typography.bodySmall,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = vm::start, enabled = !ui.running) {
                    Text(if (ui.rows.isEmpty()) "Find models" else "Test again")
                }
                if (ui.running) TextButton(onClick = vm::stop) { Text("Stop") }
                if (!ui.running && ui.working.isNotEmpty()) TextButton(onClick = vm::useFastest) { Text("Use fastest two") }
            }
            ui.note?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            ui.measuredAt?.let {
                Text("Measured at $it. Speeds change through the day.", style = MaterialTheme.typography.bodySmall)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    custom, { custom = it }, Modifier.weight(1f),
                    label = { Text("Test a model name") }, singleLine = true,
                )
                TextButton(onClick = { vm.addCustom(custom); custom = "" }, enabled = !ui.running && custom.isNotBlank()) {
                    Text("Test")
                }
            }

            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(ui.rows.sortedWith(compareBy({ order(it.probe) }, { elapsed(it.probe) })), key = { it.model }) { row ->
                    ModelCard(
                        row = row,
                        isMain = row.model == ui.main,
                        isBackup = row.model == ui.backup,
                        onMain = { vm.setMain(row.model) },
                        onBackup = { vm.setBackup(row.model) },
                    )
                }
            }
        }
    }
}

@Composable
private fun ModelCard(
    row: ModelRow,
    isMain: Boolean,
    isBackup: Boolean,
    onMain: () -> Unit,
    onBackup: () -> Unit,
) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    row.model, Modifier.weight(1f),
                    style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
                )
                if (isMain) Text("MAIN", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelSmall)
                if (isBackup) Text("BACKUP", color = MaterialTheme.colorScheme.tertiary, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelSmall)
            }
            when (val p = row.probe) {
                Probe.Pending -> Text("Waiting...", style = MaterialTheme.typography.bodySmall)
                Probe.Testing -> {
                    Text("Testing...", style = MaterialTheme.typography.bodySmall)
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                }
                is Probe.Ok -> {
                    Text("Works. Answered in ${seconds(p.ms)}", style = MaterialTheme.typography.bodyMedium)
                    Row {
                        TextButton(onClick = onMain, enabled = !isMain) { Text("Set as main") }
                        TextButton(onClick = onBackup, enabled = !isBackup) { Text("Set as backup") }
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
