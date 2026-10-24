package com.scoopz.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.scoopz.app.error.AiState
import com.scoopz.app.error.ErrorExplainer
import com.scoopz.app.error.Fix
import com.scoopz.app.error.Stage

/** Shows a saved error as: title, what happened, how to fix, plus Open Settings / Ask AI / Details. */
@Composable
fun ErrorPanel(
    stored: String?,
    stage: Stage,
    onOpenSettings: () -> Unit,
    onExplain: (Stage, String) -> Unit,
) {
    val (info, raw) = remember(stored) { ErrorExplainer.decode(stored) }
    var details by remember { mutableStateOf(false) }

    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(info.title, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.titleSmall)
        Text(info.what, style = MaterialTheme.typography.bodySmall)
        Text("Fix: ${info.fix}", style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.Medium)
        Row {
            if (info.action == Fix.OPEN_SETTINGS) TextButton(onClick = onOpenSettings) { Text("Settings") }
            if (info.aiUseful) TextButton(onClick = { onExplain(stage, raw.ifBlank { info.title }) }) { Text("Ask AI") }
            if (raw.isNotBlank()) TextButton(onClick = { details = !details }) { Text(if (details) "Hide" else "Details") }
        }
        if (details) Text(raw, style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace)
    }
}

@Composable
fun AiDialog(state: AiState?, onDismiss: () -> Unit) {
    if (state == null) return
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Close") } },
        title = { Text("AI explanation") },
        text = {
            when (state) {
                AiState.Loading -> Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(12.dp))
                    Text("Asking NVIDIA...")
                }
                is AiState.Done -> Text(state.text)
                is AiState.Failed -> Text(state.message)
            }
        },
    )
}
