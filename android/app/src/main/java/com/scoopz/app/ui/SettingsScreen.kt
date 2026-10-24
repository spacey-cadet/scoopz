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
fun SettingsScreen(onBack: () -> Unit) {
    val s = ScoopzApp.graph.settings
    var key by remember { mutableStateOf(s.apiKey) }
    var baseUrl by remember { mutableStateOf(s.baseUrl) }
    var model by remember { mutableStateOf(s.model) }
    var rpm by remember { mutableStateOf(s.requestsPerMinute.toString()) }
    var auto by remember { mutableStateOf(s.autoCaption) }

    fun save() {
        s.apiKey = key
        s.baseUrl = baseUrl
        s.model = model
        s.requestsPerMinute = rpm.toIntOrNull() ?: 30
        s.autoCaption = auto
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
            Button(onClick = { save(); onBack() }, Modifier.fillMaxWidth()) { Text("Save") }
            TextButton(onClick = {
                baseUrl = Settings.DEFAULT_BASE_URL
                model = Settings.DEFAULT_MODEL
            }) { Text("Reset endpoint and model to defaults") }
        }
    }
}
