package com.scoopz.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.scoopz.app.ScoopzApp
import com.scoopz.app.data.Settings

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(onBack: () -> Unit, onFindModels: () -> Unit) {
    val s = ScoopzApp.graph.settings
    var key by remember { mutableStateOf(s.apiKey) }
    var backup by remember { mutableStateOf(s.backupModel) }
    var textModel by remember { mutableStateOf(s.textModel) }
    var visionAlways by remember { mutableStateOf(s.visionAlways) }
    var baseUrl by remember { mutableStateOf(s.baseUrl) }
    var model by remember { mutableStateOf(s.model) }
    var rpm by remember { mutableStateOf(s.requestsPerMinute.toString()) }
    var auto by remember { mutableStateOf(s.autoCaption) }
    var twoFrames by remember { mutableStateOf(s.twoFrames) }
    var sttKey by remember { mutableStateOf(s.sttKey) }
    var sttModel by remember { mutableStateOf(s.sttModel) }
    var sttUrl by remember { mutableStateOf(s.sttBaseUrl) }

    fun save() {
        s.apiKey = key
        s.baseUrl = baseUrl
        s.model = model
        s.backupModel = backup
        s.textModel = textModel
        s.visionAlways = visionAlways
        s.requestsPerMinute = rpm.toIntOrNull() ?: 30
        s.autoCaption = auto
        s.twoFrames = twoFrames
        s.sttKey = sttKey
        s.sttModel = sttModel.ifBlank { Settings.DEFAULT_STT_MODEL }
        s.sttBaseUrl = sttUrl.ifBlank { Settings.DEFAULT_STT_BASE_URL }
    }

    BackHandler { save(); onBack() }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("Settings") },
            navigationIcon = { TextButton(onClick = { save(); onBack() }) { Text("Back") } },
        )
    }) { pad ->
        Column(
            Modifier.fillMaxSize().padding(pad).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text("Get a free key at build.nvidia.com → Settings → API Keys.")
            OutlinedTextField(
                key, { key = it }, Modifier.fillMaxWidth(),
                label = { Text("NVIDIA API key") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
            )
            OutlinedTextField(
                model, { model = it }, Modifier.fillMaxWidth(),
                label = { Text("Vision model") },
                supportingText = { Text("Must be a vision model enabled for your key") },
                singleLine = true,
            )
            OutlinedTextField(
                backup, { backup = it }, Modifier.fillMaxWidth(),
                label = { Text("Backup vision model") },
                supportingText = { Text("Used automatically if the main model is slow or fails. Blank = off") },
                singleLine = true,
            )
            OutlinedTextField(
                textModel, { textModel = it }, Modifier.fillMaxWidth(),
                label = { Text("Text model (fast captions)") },
                supportingText = { Text("Writes captions from the title, on-screen text and labels. No image is sent.") },
                singleLine = true,
            )
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("Always use the vision model (slower)", Modifier.weight(1f))
                Switch(visionAlways, { visionAlways = it })
            }
            TextButton(onClick = { save(); onFindModels() }) { Text("Find models: test which ones work and how fast") }
            OutlinedTextField(
                baseUrl, { baseUrl = it }, Modifier.fillMaxWidth(),
                label = { Text("Base URL") }, singleLine = true,
            )
            OutlinedTextField(
                rpm, { rpm = it.filter(Char::isDigit).take(2) }, Modifier.fillMaxWidth(),
                label = { Text("Max requests per minute (1–40)") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                singleLine = true,
            )
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("Caption automatically after download", Modifier.weight(1f))
                Switch(auto, { auto = it })
            }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("Send 2 frames per caption (slower, more context)", Modifier.weight(1f))
                Switch(twoFrames, { twoFrames = it })
            }
            Text("Transcription (optional, biggest caption boost)")
            Text(
                "Reads what is said in the video so captions match the content. Free key at console.groq.com. " +
                    "Leave blank to skip it.",
            )
            OutlinedTextField(
                sttKey, { sttKey = it }, Modifier.fillMaxWidth(),
                label = { Text("Transcription API key (Groq)") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                supportingText = {
                    val err = s.sttLastError
                    Text(if (err.isBlank()) "Only the audio track is uploaded, never the video." else "Last problem: $err")
                },
            )
            OutlinedTextField(
                sttModel, { sttModel = it }, Modifier.fillMaxWidth(),
                label = { Text("Transcription model") }, singleLine = true,
            )
            OutlinedTextField(
                sttUrl, { sttUrl = it }, Modifier.fillMaxWidth(),
                label = { Text("Transcription base URL") }, singleLine = true,
            )
            Button(onClick = { save(); onBack() }, Modifier.fillMaxWidth()) { Text("Save") }
            TextButton(onClick = {
                baseUrl = Settings.DEFAULT_BASE_URL
                model = Settings.DEFAULT_MODEL
                backup = Settings.DEFAULT_BACKUP_MODEL
                textModel = Settings.DEFAULT_TEXT_MODEL
                sttModel = Settings.DEFAULT_STT_MODEL
                sttUrl = Settings.DEFAULT_STT_BASE_URL
            }) { Text("Reset endpoint and model to defaults") }
        }
    }
}
