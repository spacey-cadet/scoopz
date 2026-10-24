package com.scoopz.app.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.request.ImageRequest
import coil.size.Size
import com.scoopz.app.data.CaptionState
import com.scoopz.app.data.Status
import com.scoopz.app.data.VideoEntity
import com.scoopz.app.error.Stage

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen(vm: LibraryViewModel, onOpenSettings: () -> Unit) {
    val ctx = LocalContext.current
    val videos by vm.videos.collectAsState()
    val selected by vm.selected.collectAsState()
    val ai by vm.aiState.collectAsState()
    AiDialog(ai, onDismiss = vm::dismissAi)

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (selected.isEmpty()) "Scoopz" else "${selected.size} selected") },
                actions = {
                    if (selected.isNotEmpty()) {
                        TextButton(onClick = { vm.captionSelected(); vm.clearSelection() }) { Text("Caption") }
                        TextButton(onClick = {
                            copy(ctx, videos.filter { it.sourceUrl in selected }.mapNotNull(::captionText).joinToString("\n\n"))
                        }) { Text("Copy") }
                        TextButton(onClick = vm::clearSelection) { Text("Clear") }
                    }
                    IconButton(onClick = onOpenSettings) { Icon(Icons.Default.Settings, "Settings") }
                },
            )
        },
        floatingActionButton = {
            FloatingActionButton(onClick = {
                val clip = ctx.getSystemService(ClipboardManager::class.java)
                    .primaryClip?.getItemAt(0)?.text?.toString()
                vm.addFromText(clip) { n ->
                    Toast.makeText(ctx, if (n > 0) "Queued $n link(s)" else "No new TikTok link in clipboard", Toast.LENGTH_SHORT).show()
                }
            }) { Icon(Icons.Default.Add, "Paste link") }
        },
    ) { pad ->
        if (videos.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(pad), contentAlignment = Alignment.Center) {
                Text(
                    "Share a TikTok link to Scoopz,\nor tap + to paste one.",
                    style = MaterialTheme.typography.bodyLarge,
                )
            }
        } else {
            LazyColumn(
                Modifier.fillMaxSize().padding(pad),
                contentPadding = PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                items(videos, key = { it.sourceUrl }) { v ->
                    VideoCard(
                        v = v,
                        isSelected = v.sourceUrl in selected,
                        onToggle = { vm.toggleSelected(v.sourceUrl) },
                        onCaption = { vm.caption(v.sourceUrl) },
                        onRetry = { vm.retry(v.sourceUrl) },
                        onDelete = { vm.delete(v) },
                        onCopy = { copy(ctx, captionText(v).orEmpty()) },
                        onOpenSettings = onOpenSettings,
                        onExplain = vm::explainWithAi,
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun VideoCard(
    v: VideoEntity,
    isSelected: Boolean,
    onToggle: () -> Unit,
    onCaption: () -> Unit,
    onRetry: () -> Unit,
    onDelete: () -> Unit,
    onCopy: () -> Unit,
    onOpenSettings: () -> Unit,
    onExplain: (Stage, String) -> Unit,
) {
    val ctx = LocalContext.current
    Card(
        Modifier.fillMaxWidth().combinedClickable(onClick = onToggle, onLongClick = onToggle),
        colors = CardDefaults.cardColors(
            containerColor = if (isSelected) MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.surfaceVariant
        ),
    ) {
        Row(Modifier.padding(10.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            if (v.mediaUri != null) {
                AsyncImage(
                    model = ImageRequest.Builder(ctx).data(v.mediaUri).size(Size(256, 256)).build(),
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.size(72.dp, 96.dp),
                )
            } else {
                Box(Modifier.size(72.dp, 96.dp))
            }
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    v.author?.let { "@$it" } ?: v.sourceUrl,
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                when (v.status) {
                    Status.QUEUED -> Text("Queued…", style = MaterialTheme.typography.bodySmall)
                    Status.DOWNLOADING -> {
                        Text("Downloading…", style = MaterialTheme.typography.bodySmall)
                        LinearProgressIndicator(Modifier.fillMaxWidth())
                    }
                    Status.FAILED -> {
                        ErrorPanel(v.error, Stage.DOWNLOAD, onOpenSettings, onExplain)
                        Row {
                            TextButton(onClick = onRetry) { Text("Retry") }
                            TextButton(onClick = onDelete) { Text("Remove") }
                        }
                    }
                    Status.DONE -> {
                        when (v.captionState) {
                            CaptionState.RUNNING -> {
                                Text("Captioning…", style = MaterialTheme.typography.bodySmall)
                                LinearProgressIndicator(Modifier.fillMaxWidth())
                            }
                            CaptionState.DONE -> Text(
                                captionText(v).orEmpty(),
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = 6,
                                overflow = TextOverflow.Ellipsis,
                            )
                            CaptionState.FAILED -> ErrorPanel(v.error, Stage.CAPTION, onOpenSettings, onExplain)
                            CaptionState.NONE -> Text("No caption yet", style = MaterialTheme.typography.bodySmall)
                        }
                        Row {
                            TextButton(onClick = onCaption) {
                                Text(if (v.captionState == CaptionState.DONE) "Redo" else "Caption")
                            }
                            if (v.captionState == CaptionState.DONE) TextButton(onClick = onCopy) { Text("Copy") }
                            TextButton(onClick = onDelete) { Text("Delete") }
                        }
                    }
                }
            }
        }
    }
}

private fun captionText(v: VideoEntity): String? =
    v.caption?.let { c -> if (v.hashtags.isNullOrBlank()) c else "$c\n\n${v.hashtags}" }

private fun copy(ctx: Context, text: String) {
    if (text.isBlank()) return
    ctx.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("caption", text))
    Toast.makeText(ctx, "Copied", Toast.LENGTH_SHORT).show()
}
